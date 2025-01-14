import os.path as osp

import json
import random
import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.cuda.amp import GradScaler

from dassl.engine import TRAINER_REGISTRY, TrainerX
from dassl.metrics import compute_accuracy
from dassl.utils import load_pretrained_weights, load_checkpoint
from dassl.optim import build_optimizer, build_lr_scheduler

from clip import clip
import torch.utils.checkpoint as checkpoint

def load_clip_to_cpu(cfg):
    backbone_name = cfg.MODEL.BACKBONE.NAME
    url = clip._MODELS[backbone_name]
    model_path = clip._download(url)

    try:
        # loading JIT archive
        model = torch.jit.load(model_path, map_location="cpu").eval()
        state_dict = None

    except RuntimeError:
        state_dict = torch.load(model_path, map_location="cpu")
    
    model = clip.build_model(state_dict or model.state_dict())

    return model


class VisionEncoderZS(nn.Module):
    def __init__(self, cfg, clip_model):
        super().__init__()

        visual = clip_model.visual
        self.ln_pre = visual.ln_pre
        self.transformer = visual.transformer.resblocks
        self.ln_post = visual.ln_post
        self.proj = visual.proj
        self.dtype = clip_model.dtype
        self.conv1 = clip_model.visual.conv1
        self.class_embedding = clip_model.visual.class_embedding
        self.positional_embedding = clip_model.visual.positional_embedding

    def forward(self, x):
        x = self.conv1(x)
        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)

        x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)
        x = self.ln_pre(x).type(self.dtype)
        x = x.permute(1, 0, 2)

        x = self.transformer(x)
        
        x = x.permute(1, 0, 2)
        x = self.ln_post(x[:, 0, :])
        x = x @ self.proj
        
        return x


class VisionEncoder(nn.Module):
    def __init__(self, cfg, clip_model):
        super().__init__()
        visual = clip_model.visual
        self.ln_pre = visual.ln_pre
        self.transformer = visual.transformer.resblocks
        self.ln_post = visual.ln_post
        self.proj = visual.proj
        self.dtype = clip_model.dtype
        self.n_vpro = cfg.TRAINER.DAM.N_VPRO # prompt length

    def forward(self, x, p_visual):
        x = self.ln_pre(x).type(self.dtype)
        x = x.permute(1, 0, 2)

        for layer_idx, layer in enumerate(self.transformer):
            if layer_idx > 0:
                # insert layer-wise global visual prompt
                x[-self.n_vpro:] = p_visual[layer_idx-1].unsqueeze(1).expand(-1, x.shape[1], -1)
            x = layer(x)
            
        x = x.permute(1, 0, 2)
        x = self.ln_post(x[:, 0, :])
        x = x @ self.proj

        return x


class VisionPromptLearner(nn.Module):
    def __init__(self, cfg, clip_model):
        super().__init__()
        self.n_vpro = cfg.TRAINER.DAM.N_VPRO
        self.pro_dim = clip_model.visual.ln_pre.weight.shape[0]
        self.dtype = clip_model.dtype
        self.conv1 = clip_model.visual.conv1
        self.class_embedding = clip_model.visual.class_embedding
        self.positional_embedding = clip_model.visual.positional_embedding
        self.layers = len(clip_model.visual.transformer.resblocks)
        # global prompt for image encoder (except for the first layer)
        self.p_visual = nn.ParameterList([nn.Parameter(torch.empty(self.n_vpro, self.pro_dim).type(self.dtype))
                                          for _ in range(self.layers-1)])
        for p in self.p_visual:
            nn.init.normal_(p, std=0.02)
            
        # global prompt for the first layer of image encoder
        self.p_input = nn.Parameter(torch.empty(self.n_vpro, self.pro_dim))
        nn.init.normal_(self.p_input, std=0.02)

    def forward(self, x):
        x = x.type(self.dtype)
        x = self.conv1(x)
        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)
        x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], 
                                                                      dtype=x.dtype, device=x.device), x], dim=1) 
        x = x + self.positional_embedding.to(x.dtype)
        
        # insert global visual prompt of the first layer
        p_input = self.p_input.unsqueeze(0).expand(len(x), -1, -1)
        x = torch.cat([x, p_input], dim=1)

        return x, self.p_visual


class TextEncoderZS(nn.Module):
    def __init__(self, cfg, clip_model):
        super().__init__()
        self.transformer = clip_model.transformer.resblocks
        self.positional_embedding = clip_model.positional_embedding
        self.ln_final = clip_model.ln_final
        self.text_projection = clip_model.text_projection
        self.dtype = clip_model.dtype
        self.token_embedding = clip_model.token_embedding

    def forward(self, text):
        x = self.token_embedding(text).type(self.dtype)
        x = x + self.positional_embedding.type(self.dtype)
        x = x.permute(1, 0, 2)
        
        feats = []
        for _, layer in enumerate(self.transformer):
            x = layer(x)
            # save class embeddings from different layers
            feats.append(x[text.argmax(dim=-1), torch.arange(x.shape[1])])

        x = x.permute(1, 0, 2)
        x = self.ln_final(x)
        x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.text_projection
        txt_feats = torch.stack(feats)

        return x, txt_feats


class TextEncoder(nn.Module):
    def __init__(self, cfg, clip_model):
        super().__init__()
        self.transformer = clip_model.transformer.resblocks
        self.positional_embedding = clip_model.positional_embedding
        self.ln_final = clip_model.ln_final
        self.text_projection = clip_model.text_projection
        self.dtype = clip_model.dtype
        self.n_tpro = cfg.TRAINER.DAM.N_TPRO # prompt length
        self.n_set = cfg.TRAINER.DAM.N_SET # number of descriptions for each category

    def forward(self, x, p_ins, p_uni, tokenized_prompts, attn, flag):
        # p_ins: instance-specific prompt, a.k.a high-level prompt from descriptions
        # p_uni: task-unified prompt, a.k.a global-level prompt
        # flag: True when training and False when testing
        # Since we use all (self.n_set) descriptions for learning high-level prompt, we should reshape p_ins first.
        (l, c, d) = p_ins.shape
        p_ins = p_ins.reshape(l, c//self.n_set, self.n_set, d) # (L, C, n_set, D)

        # During evaluation, we leverage all (n_set) structures according to descriptions for modeling one category (N*C*n_set steps in total), 
        # instead of randomly picking one structure for each category (N*C steps in one epoch). 
        if not flag:
            p_ins = p_ins.unsqueeze(2).expand(-1, -1, self.n_set, -1, -1)
            p_ins = torch.flatten(p_ins, 1, 2) # (L, C*n_set, n_set, D)
            
        p_ins = p_ins.permute(0, 2, 1, 3).type(self.dtype)
        x = (x + self.positional_embedding).type(self.dtype)
        x = x.permute(1, 0, 2)

        for layer_idx, layer in enumerate(self.transformer):
            if layer_idx > 0:                
                prefix = x[:1]
                suffix = x[1+self.n_tpro+self.n_set:]
                
                # global-level prompt
                ctx_g = p_uni[layer_idx - 1].unsqueeze(1).expand(self.n_tpro, prefix.shape[1], -1)
                
                # high-level prompt
                ctx_h = p_ins[layer_idx - 1]
                x = torch.cat([prefix, ctx_g, ctx_h, suffix], dim=0)
                
                # 'attn' is attention matrix from topological prompt learner, 
                # considering as low-level prompt which models relationships in an explicit way.
                x = layer(x, attn[:, layer_idx])
            elif layer_idx == 0:
                x = layer(x, attn[:, layer_idx])
            else:
                x = layer(x)

        x = x.permute(1, 0, 2)
        x = self.ln_final(x)
        x = x[torch.arange(x.shape[0]), tokenized_prompts.argmax(dim=-1)] @ self.text_projection
        
        if not flag:
            x = x.reshape(x.shape[0]//5, 5, -1)

        return x


class PromptLearner(nn.Module):
    def __init__(self, cfg, classnames, info_topo, clip_model):
        super().__init__()
        self.n_tpro = cfg.TRAINER.DAM.N_TPRO # prompt length
        self.n_set = cfg.TRAINER.DAM.N_SET # number of descriptions for each category
        self.dtype = clip_model.dtype
        self.ctx_dim = clip_model.ln_final.weight.shape[0]
        self.layers = len(clip_model.transformer.resblocks)

        # global prompt for text encoder (except for the first layer)
        self.p_uni = nn.ParameterList([nn.Parameter(torch.empty(self.n_tpro, self.ctx_dim).type(self.dtype))
                                                      for _ in range(self.layers - 1)])
        for p in self.p_uni:
            nn.init.normal_(p, std=0.02)
            
        # projector for learning high-level prompt (a.k.a p_ins)
        self.p_ins_projector = nn.Linear(self.ctx_dim, self.ctx_dim)
        
        # global prompt for the first layer of the text encoder
        self.p_input = nn.Parameter(torch.empty(self.n_tpro+self.n_set, self.ctx_dim))
        nn.init.normal_(self.p_input, std=0.02)
        
        self.classnames = [name.replace("_", " ") for name in classnames]
        self.info_topo = info_topo # topological structure in a dictionary form
        self.n_cls = len(classnames)
        self.clip_model = clip_model
    def forward(self, feats, attns, flag):
        p_uni = self.p_uni
        prompts, attn = [], []
        prompt_prefix = " ".join(["X"] * (self.n_tpro+self.n_set))

        if flag:
            for name in self.classnames:
                # For efficiency, we randomly pick one structure as a part of input during training, 
                # while leveraging all descriptions of the category for learning high-level prompt.
                id = random.randint(0, self.n_set-1)
                topo = self.info_topo[name][id]
                p = prompt_prefix + " " + name + ". " + ", ".join(topo['Entities']) + ". " + ", ".join(topo['Attributes']) + "."
                attn.append(attns[name][:, id])
                prompts.append(p)
        else:
            for name in self.classnames:
                # We leverage all structures from descriptions as a part of input respectively during evaluation.
                for id in range(self.n_set):
                    topo = self.info_topo[name][id]
                    p = prompt_prefix + " " + name + ". " + ", ".join(topo['Entities']) + ". "  + ", ".join(topo['Attributes']) + "." 
                    attn.append(attns[name][:, id])
                    prompts.append(p)
        
        attn = torch.stack(attn, dim=0)
            
        self.tokenized_prompts = torch.cat([clip.tokenize(p, truncate=True) for p in prompts]).cuda()  # (n_cls, n_tkn)
        with torch.no_grad():
            embedding = self.clip_model.token_embedding(self.tokenized_prompts).type(self.dtype)
        
        p_input = self.p_input.unsqueeze(0).expand(len(prompts), -1, -1)
        prefix = embedding[:, :1]
        suffix = embedding[:, 1+self.n_tpro+self.n_set:]
        
        # the input of the prompted text encoder
        p_ori = torch.cat([prefix, p_input, suffix], dim=1)

        # generate corresponding high-level prompt (p_ins)
        p_ins = []
        (l, c, n, d) = feats.shape
        feats = feats.reshape(l, c*n, d)
        for idx in range(self.layers - 1):
            feat = feats[idx].float()
            feat = feat + self.p_ins_projector(feat) 
            p_ins.append(feat)
        p_ins = torch.stack(p_ins, dim=0)

        return p_ori, p_ins, p_uni, attn

    
class TopoPromptLearner(nn.Module):
    def __init__(self, cfg, classnames, prompt_topo, clip_model):
        super().__init__()

        self.classnames = classnames
        self.dtype = clip_model.dtype
        self.n_set = cfg.TRAINER.DAM.N_SET # number of descriptions for each category
        self.n_tpro = cfg.TRAINER.DAM.N_TPRO # prompt length
        self.layers = len(clip_model.transformer.resblocks)
        
        # layer-wise scalar to weight indicating the strength of the relationship of entity-entity pairs and entity-attribute pairs
        self.e2e_scal = nn.Parameter(torch.zeros(self.layers, 1, 1, 1)) 
        self.e2a_scal = nn.Parameter(torch.zeros(self.layers, 1, 1, 1))
        
        self.attns_e2e = {classname: [] for classname in classnames}
        self.attns_e2a = {classname: [] for classname in classnames}

        prompt_prefix = " ".join(["X"] * (self.n_tpro + self.n_set))

        for classname in classnames:
            topos = prompt_topo[classname]
            for id in range(self.n_set):
                # generate text with classname, entities and attributes
                txt = self.generate_text(classname, prompt_prefix, topos[id])
                tokens = clip.tokenize(txt, truncate=True)[0]
                
                # generate pair-wise relationships
                e2e, e2a = self.extract_relationships(tokens, topos[id])

                # create attention matrix based on pair-wise relationships
                attn_e2e = self.create_attention_matrix(tokens, e2e)
                attn_e2a = self.create_attention_matrix(tokens, e2a)

                # save attention matrices
                self.attns_e2e[classname].append(attn_e2e)
                self.attns_e2a[classname].append(attn_e2a)

    # generate text with classname, entities and attributes
    def generate_text(self, classname, prompt_prefix, topo):
        entities = [w.lower() for w in topo['Entities']]
        attributes = [w.lower() for w in topo['Attributes']]
        txt = prompt_prefix + " " + classname + ". " + ", ".join(entities) + ". " + ", ".join(attributes) + "."
        return txt

    # generate pair-wise relationships from topological structure
    def extract_relationships(self, tokens, topo):
        entities = [w.lower() for w in topo['Entities']]
        attributes = [w.lower() for w in topo['Attributes']]
        e2e, e2a = [], []

        for w in topo['Entity-to-Entity Relationships']:
            if w['entity1'].lower() in entities and w['entity2'].lower() in entities:
                e1 = list(self.align(tokens, self.truncate(clip.tokenize(w['entity1']))[0]))
                e2 = list(self.align(tokens, self.truncate(clip.tokenize(w['entity2']))[0]))
                e2e.append([e1, e2])

        for w in topo['Entity-to-Attribute Relationships']:
            if w['entity'].lower() in entities and w['attribute'].lower() in attributes:
                e1 = list(self.align(tokens, self.truncate(clip.tokenize(w['entity']))[0]))
                e2 = list(self.align(tokens, self.truncate(clip.tokenize(w['attribute']))[0]))
                e2a.append([e1, e2])

        return e2e, e2a

    # create attention matrix based on pair-wise relationships
    def create_attention_matrix(self, tokens, relationships):
        n_tokens = len(tokens)
        attn = torch.zeros(n_tokens, n_tokens).cuda()

        for e in relationships:
            d11 = torch.tensor([[i] for i in e[0]]).type(torch.long)
            d21 = torch.tensor([e[1] for _ in range(len(e[0]))]).type(torch.long)
            d12 = torch.tensor([[i] for i in e[1]]).type(torch.long)
            d22 = torch.tensor([e[0] for _ in range(len(e[1]))]).type(torch.long)
            attn[d11, d21] += 1
            attn[d12, d22] += 1

        return attn

    # truncate token sequence according to EOS token
    def truncate(self, array):
        return array[:, 1:torch.argmax(array)]

    # find a sequence that matches the target token(s)
    def align(self, seq1, seq2):
        for idx in range(len(seq1) - len(seq2) + 1):
            if seq1[idx:idx + len(seq2)].equal(seq2):
                return range(idx, idx + len(seq2))
        return []

    def forward(self):
        attns = {}
        for classname in self.classnames:
            classname = classname.replace("_", " ")
            # weight generated matrices with two learnable scalars
            attns[classname] = self.e2e_scal * torch.stack(self.attns_e2e[classname]).cuda() + \
                               self.e2a_scal * torch.stack(self.attns_e2a[classname]).cuda()
        return attns

class CrossModalAlignment(nn.Module):
    # img_f: B, K, 512
    # text_f: N, K, 512
    # K=1.
    # When self.cfg.XD is True (indicating cross-dataset or domain generalization tasks).
    # To address the computational burden caused by utilizing all categories in 
    # cross-dataset and domain generalization tasks, 
    # we have segmented the computation of W_T into "number_class // chunk_size" parts. 
    # This partitioning allows for individual calculations, thereby reducing computational costs.
    
    def __init__(self, cfg):
        super().__init__()
        self.r = nn.Parameter(torch.zeros(4))
        self.alp = nn.Parameter(torch.FloatTensor([0.5]))
        self.scale = nn.Parameter(torch.FloatTensor([1.0]))
        self.logits_scales = nn.Parameter(torch.FloatTensor([0.5]))
        self.cfg = cfg
        
    def get_rc_dist_ItoT(self, img_f, text_f, alpha, beta, chunk_size):
        B, K, N, d = img_f.shape[0], img_f.shape[1], text_f.shape[0], text_f.shape[-1]

        I = img_f.float()   # (B, K, 512)
        T = text_f.float()  # (N, K, 512)

        reg = I.shape[0] / I.shape[2]
        lam = reg * alpha.exp() + 1e-6
        rho = beta.exp().float()
       
        It = I.permute(0, 2, 1)     # (B, 512, K)

        ItI = It.matmul(I)      # (B, 512, 512)
        M_inv = (ItI + torch.eye(ItI.size(-1)).to(ItI.device).unsqueeze(0).mul(lam)).inverse()      # (B, 512, 512)
        A = M_inv.matmul(It) 

        if self.cfg.XD:
            dist = torch.zeros((B, N)).to(I.device)
            for i in range(0, N, chunk_size):
                with torch.no_grad():
                    T_chunk = T[i:i+chunk_size]  # (chunk_size, K, 512)
                    T_expanded = T_chunk.unsqueeze(0)  # (1, chunk_size, K, 512)
                    W_chunk = A.unsqueeze(1).matmul(T_expanded)  # (B, chunk_size, 512, 512)

                    T_bar_chunk = I.unsqueeze(1).matmul(W_chunk).mul(rho)  # (B, chunk_size, 1, 512)
                    dist_chunk = (T_bar_chunk - T_chunk.unsqueeze(0)).pow(2).sum(dim=-1).neg().view(B, T_bar_chunk.size(1), K).mean(dim=-1)  # (B, chunk_size)

                    dist[:, i:i+chunk_size] = dist_chunk
        else:
            A_expanded = A.unsqueeze(1)     # (B, 1, c, 1)
            T_expanded = T.unsqueeze(0)     # (1, N, 1, c)
            
            W = A_expanded.matmul(T_expanded) # (B, N, c, c)

            T_bar = I.unsqueeze(1).matmul(W).mul(rho) # (B, N, 1, 512)
            dist = (T_bar - T.unsqueeze(0)).pow(2).sum(dim=-1).neg().view(B, N, K).mean(dim=-1) # (B, N)
        
        return dist
    
    def get_rc_dist_TtoI(self, img_f, text_f, alpha, beta, chunk_size):
        B, K, N, d = img_f.shape[0], img_f.shape[1], text_f.shape[0], img_f.shape[-1]

        I = img_f.float() # (B, 1, 512)
        T = text_f.float() # (N, 1, 512)

        reg = T.shape[0] / T.shape[2]
        lam = reg * alpha.exp() + 1e-6
        rho = beta.exp().float()

        if self.cfg.XD:
            dist = torch.zeros((N, B)).to(I.device)
            for i in range(0, N, chunk_size):
                with torch.no_grad():
                    T_chunk = T[i:i+chunk_size]  # (chunk_size, 1, 512)
                    Tt = T_chunk.permute(0, 2, 1) # (chunk_size, 512, 1)
                    TtT = Tt.matmul(T_chunk) # (chunk_size, 512, 512)

                M_inv = (TtT + torch.eye(TtT.size(-1)).to(TtT.device).unsqueeze(0).mul(lam)).inverse() # (chunk_size, 512, 512)
                
                with torch.no_grad():
                    A = M_inv.matmul(Tt)

                    A_expanded = A.unsqueeze(1)     # (chunk_size, 1, c, 1)
                    I_expanded = I.unsqueeze(0)     # (1, B, 1, c)

                    W = A_expanded.matmul(I_expanded) # (chunk_size, B, c, c)

                I_bar = T_chunk.unsqueeze(1).matmul(W).mul(rho) # (chunk_size, B, 1, 512)
                dist_chunk = (I_bar - I.unsqueeze(0)).pow(2).sum(dim=-1).neg().view(I_bar.size(0), B, K).mean(dim=-1).t() # (B, chunk_size)
                dist[i:i+chunk_size] = dist_chunk.t() # (chunk_size, B)

            dist = dist.t() # (B, N)
        
        else: 
            Tt = T.permute(0, 2, 1) # (N, 512, 1)

            TtT = Tt.matmul(T) # (N, 512, 512)
            M_inv = (TtT + torch.eye(TtT.size(-1)).to(TtT.device).unsqueeze(0).mul(lam)).inverse() # (N, 512, 512)
            A = M_inv.matmul(Tt)

            A_expanded = A.unsqueeze(1)     # (N, 1, c, 1)
            I_expanded = I.unsqueeze(0)     # (1, B, 1, c)

            W = A_expanded.matmul(I_expanded) # (N, B, c, c)

            I_bar = T.unsqueeze(1).matmul(W).mul(rho) # (B, N, 1, 512)
            dist = (I_bar - I.unsqueeze(0)).pow(2).sum(dim=-1).neg().view(N, B, K).mean(dim=-1).t()

        return dist
    
    def forward(self, img_f, text_f):
        alpha_ItoT, alpha_TtoI = self.r[0], self.r[2]
        beta_ItoT, beta_TtoI = self.r[1], self.r[3]  
        alp = self.alp
        chunk_size = 100

        rc_dist_ItoT = self.get_rc_dist_ItoT(img_f, text_f, alpha_ItoT, beta_ItoT, chunk_size)
        rc_dist_TtoI = self.get_rc_dist_TtoI(img_f, text_f, alpha_TtoI, beta_TtoI, chunk_size)
        rc_dist = alp * rc_dist_ItoT + (1 - alp) * rc_dist_TtoI

        logits = rc_dist*self.scale
        logits_pre = F.log_softmax(logits, dim=1)

        return logits_pre, self.logits_scales.sigmoid()


class text_xd_SameModalAlignment(nn.Module):
    # To save computational costs in cross-dataset and domain generation tasks, 
    # we have designed the text_xd_SameModalAlignment method, 
    # which generates a single C×C intra-modal alignment weight for all text categories.
    def __init__(self, cfg):
        super().__init__()
        self.alpha = nn.Parameter(torch.zeros(1))
        self.beta = nn.Parameter(torch.FloatTensor([0.5]))

    def get_align_dist(self, source, target, alpha):
        S = source.float()  # (N, 512)
        T = target.float()  # (N, 512)
        reg = S.shape[0] / S.shape[1]
        lam = reg * alpha.exp() + 1e-6

        St = S.t()  # (512, N)
        StS = St.matmul(S)  # (512, 512)
        M_inv = (StS + torch.eye(StS.size(-1)).to(StS.device).mul(lam)).inverse()  # (512, 512)
        W = M_inv.matmul(St).matmul(T)  # (512, 512)

        return W

    def forward(self, source, target):

        alpha = self.alpha
        W = self.get_align_dist(source, target, alpha)
        identity = torch.eye(W.size(0)).to(W.device)
        W = W - (W - identity) * self.beta

        return W
    
class SameModalAlignment(nn.Module):
    # For each frozen and prompted image or text pair, 
    # we use SMA to generate a C×C weight to align the prompted features to the frozen features.
    # In text case, we replace B with N.
    def __init__(self, cfg):
        super().__init__()
        self.alpha = nn.Parameter(torch.zeros(1))
        self.beta = nn.Parameter(torch.FloatTensor([0.5]))

    def get_align_dist(self, source, target, alpha):
        S = source.float()  # (B, 1, 512)
        T = target.float()  # (B, 1, 512)
        reg = S.shape[0] / S.shape[2]
        lam = reg * alpha.exp() + 1e-6

        St = S.permute(0, 2, 1)  # (B, 512, 1)
        StS = St.matmul(S)  # (B, 512, 512)
        M_inv = (StS + torch.eye(StS.size(-1)).to(StS.device).mul(lam)).inverse()  # (B, 512, 512)
        W = M_inv.matmul(St).matmul(T)  # (B, 512, 512)

        return W
    

    def forward(self, source, target):
        B = source.shape[0]
        alpha = self.alpha

        W = self.get_align_dist(source, target, alpha)
        identity = torch.eye(W.size(1)).unsqueeze(0).repeat(B, 1, 1).to(W.device)
        W = W - (W - identity) * self.beta
        
        return W

class CustomCLIP(nn.Module):
    def __init__(self, cfg, classnames, clip_model):
        super().__init__()
        for p in clip_model.parameters():
            p.requires_grad = False

        if 'ImageNet' not in cfg.DATASET.NAME:
            dname = cfg.DATASET.NAME
        else:
            dname = 'ImageNet'
            
        f_json = osp.join(cfg.DATASET.GPT_DIR+'/description', dname+'.json')
        f_topo = osp.join(cfg.DATASET.GPT_DIR+'/structure', dname+'.json')
        
        with open(f_json, 'r') as f:
            text_prompts = json.load(f)
        with open(f_topo, 'r') as f:
            text_topos = json.load(f)

        classnames = [name.replace("_", " ") for name in classnames]
        self.topo_prompt_learner = TopoPromptLearner(cfg, classnames, text_topos, clip_model)
        self.prompt_learner = PromptLearner(cfg, classnames, text_topos, clip_model)
        self.vision_prompt_learner = VisionPromptLearner(cfg, clip_model)
        self.image_encoder = VisionEncoder(cfg, clip_model)
        self.text_encoder = TextEncoder(cfg, clip_model)
        self.text_encoder_zs = TextEncoderZS(cfg, clip_model)
        self.image_encoder_zs = VisionEncoderZS(cfg, clip_model)
        self.logit_scale = clip_model.logit_scale
        self.dtype = clip_model.dtype
        self.model = clip_model
        self.cfg = cfg

        self.cma = CrossModalAlignment(cfg)
        self.img_sma = SameModalAlignment(cfg)
        if cfg.XD:
            self.text_sma = text_xd_SameModalAlignment(cfg)
        else:
            self.text_sma = SameModalAlignment(cfg)
        self.flag = True

        with torch.no_grad():
            # zs_feats: layer-wise class embeddings from frozen text encoder
            # zs_repres: final representations from frozen text encoder
            zs_feats, zs_repres = [], []
            for classname in classnames:
                texts = text_prompts[classname]
                texts = clip.tokenize(texts).cuda()
                class_embeddings, features = self.text_encoder_zs(texts)
                class_embeddings /= class_embeddings.norm(dim=-1, keepdim=True)
                class_embedding = class_embeddings.mean(dim=0)
                class_embedding /= class_embedding.norm()
                features /= features.norm(dim=-1, keepdim=True)
                zs_feats.append(features)
                zs_repres.append(class_embedding)
            self.text_features_zs = torch.stack(zs_repres, dim=1).cuda()
            self.text_features_ft = torch.stack(zs_feats, dim=1).cuda()

        self.image_align_m = cfg.TRAINER.I_M
        self.text_align_m = cfg.TRAINER.T_M
        self.w = cfg.TRAINER.W
        print("image_align_m:", self.image_align_m, "   text_align_m:", self.text_align_m, "   loss_w:", self.w)

    def forward(self, image, image2=None, label=None):
        logit_scale = self.logit_scale.exp()
        
        text_features_zs = self.text_features_zs    # D N
        if image2 is None:
            image2 = image
        image_features_zs = self.image_encoder_zs(image2.type(self.dtype))
        image_features_zs = image_features_zs / image_features_zs.norm(dim=-1, keepdim=True)    # B D
        
        attns = self.topo_prompt_learner()
        p_ori, p_ins, p_uni, attns = self.prompt_learner(self.text_features_ft, attns, self.training)

        tokenized_prompts = self.prompt_learner.tokenized_prompts
        text_features = self.text_encoder(p_ori, p_ins, p_uni, tokenized_prompts, attns, self.training)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        
        # Since we use multiple structures for producing representations of one category, 
        # we should take their mean value as the final representation.
        if not self.training:
            text_features = text_features.mean(dim=1)
        
        x, p_visual = self.vision_prompt_learner(image)
        image_features = self.image_encoder(x, p_visual)

        image_features = image_features / image_features.norm(dim=-1, keepdim=True) # B D
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)    # N D
        
        # Unlike the approach outlined in the paper, we transpose the features for text and image, 
        # as described in the paper. We substitute the paper's $f$ with $f^T$, which is equivalent.
        img_weight = self.img_sma(image_features.unsqueeze(1), image_features_zs.unsqueeze(1))
        x_a = image_features.unsqueeze(1).float().matmul(img_weight).squeeze(1)

        if self.cfg.XD:
            text_weight = self.text_sma(text_features, text_features_zs.t())
            x_b = text_features.float().matmul(text_weight)
        else:
            text_weight = self.text_sma(text_features.unsqueeze(1), text_features_zs.t().unsqueeze(1))
            x_b = text_features.unsqueeze(1).float().matmul(text_weight).squeeze(1)

        
        image_features = self.image_align_m * x_a + image_features
        image_features = image_features / image_features.norm(dim=-1, keepdim=True) 
        
        text_features = self.text_align_m * x_b + text_features
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

        # asymmetric loss
        logits_org = logit_scale * (image_features @ text_features.t())
        logits_cma, logits_scale = self.cma(image_features.unsqueeze(1), text_features.unsqueeze(1))
        logits_cma = logit_scale * logits_cma
        logits = logits_scale*logits_org + (1-logits_scale)*logits_cma

        if self.cfg.CD:
            logits_i = logit_scale * (image_features @ text_features_zs.float())
            logits_t = logit_scale * (image_features_zs.float() @ text_features.t())
            logits = (logits + logits_i + logits_t)/3
        

        if self.training:
            cos = torch.nn.CosineSimilarity(dim=1,eps=1e-07)
            score = cos(image_features, image_features_zs)
            loss_smr_image = 1.0 - torch.mean(score)

            score = cos(text_features, text_features_zs.t())
            loss_smr_text = 1.0 - torch.mean(score)

            loss_cmr = F.cross_entropy(logits, label)
            
            loss = loss_cmr + self.w*(loss_smr_image + loss_smr_text)
            # loss = F.cross_entropy(logits, label)

            return logits, loss
        else:
            return logits


@TRAINER_REGISTRY.register()
class DAM(TrainerX):
    def check_cfg(self, cfg):
        assert cfg.TRAINER.DAM.PREC in ["fp16", "fp32", "amp"]

    def build_model(self):
        cfg = self.cfg
        classnames = self.dm.dataset.classnames
        self.n_class = len(self.dm.dataset.classnames)

        print(f"Loading CLIP (backbone: {cfg.MODEL.BACKBONE.NAME})")
        clip_model = load_clip_to_cpu(cfg).cuda()

        if cfg.TRAINER.DAM.PREC == "fp32" or cfg.TRAINER.DAM.PREC == "amp":
            # CLIP's default precision is fp16
            clip_model.float()

        print("Building custom CLIP")
        self.model = CustomCLIP(cfg, classnames, clip_model)

        print("Turning off gradients in both the image and the text encoder")

        for name, param in self.model.named_parameters():
            if "prompt_learner" not in name and "cma" not in name and "sma" not in name:
                param.requires_grad_(False)

        # Double check
        enabled = set()
        for name, param in self.model.named_parameters():
            if param.requires_grad:
                enabled.add(name)
        print(f"Parameters to be updated: {enabled}")

        if cfg.MODEL.INIT_WEIGHTS:
            load_pretrained_weights(self.model, cfg.MODEL.INIT_WEIGHTS)

        self.model.to(self.device)

        self.optim = build_optimizer(self.model, cfg.OPTIM)
        self.sched = build_lr_scheduler(self.optim, cfg.OPTIM)
        self.register_model("Model", self.model, self.optim, self.sched)

        self.scaler = GradScaler() if cfg.TRAINER.DAM.PREC == "amp" else None

    def forward_backward(self, batch):
        image1, image2, label = self.parse_batch_train(batch)

        logits, loss = self.model(image1, image2, label)

        self.model_backward_and_update(loss)

        loss_summary = {
            "loss": loss.item(),
            "acc": compute_accuracy(logits, label)[0].item(),
        }

        if (self.batch_idx + 1) == self.num_batches:
            self.update_lr()

        return loss_summary

    def parse_batch_train(self, batch):
        input = batch["img"]
        image1, image2 = input[0], input[1]
        label = batch["label"]
        image1 = image1.to(self.device)
        image2 = image2.to(self.device)
        label = label.to(self.device)
        return image1, image2, label

    def load_model(self, directory, epoch=None):
        if not directory:
            print("Note that load_model() is skipped as no pretrained model is given")
            return

        names = self.get_model_names()

        # By default, the best model is loaded
        model_file = "model-best.pth.tar"

        if epoch is not None:
            model_file = "model.pth.tar-" + str(epoch)

        for name in names:
            model_path = osp.join(directory, name, model_file)

            if not osp.exists(model_path):
                raise FileNotFoundError('Model not found at "{}"'.format(model_path))

            checkpoint = load_checkpoint(model_path)
            state_dict = checkpoint["state_dict"]
            epoch = checkpoint["epoch"]

            print("Loading weights to {} " 'from "{}" (epoch = {})'.format(name, model_path, epoch))
            # set strict=False
            self._models[name].load_state_dict(state_dict, strict=False)
