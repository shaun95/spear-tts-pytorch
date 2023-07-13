# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/A. Neural modules.ipynb.

# %% auto 0
__all__ = ['LayerNorm', 'LinearHead', 'QueryHead', 'init_transformer', 'sinusoids', 'MultiHeadAttention',
           'ResidualAttentionBlock', 'Encoder', 'Decoder', 'SumDecoder']

# %% ../nbs/A. Neural modules.ipynb 2
import torch
import numpy as np
import math

from torch import Tensor, nn
import torch.nn.functional as F
from typing import Dict, Iterable, Optional

import xformers.ops as xops

# %% ../nbs/A. Neural modules.ipynb 3
# Code in this file is mostly borrowed from
# https://github.com/openai/whisper/blob/main/whisper/model.py
# and is under the MIT License

class LayerNorm(nn.LayerNorm):
    def forward(self, x):
        return super().forward(x.float()).type(x.dtype)

# Used in μP to initialize the weights and configure the optimizer
# These two layers map the transformer width into a fixed dimension
class LinearHead(nn.Linear):
    pass

class QueryHead(nn.Linear):
    pass

# based on https://github.com/karpathy/minGPT/blob/master/mingpt/model.py#L163
def init_transformer(m):
    if isinstance(m, (nn.Linear, nn.Embedding)):
        torch.nn.init.trunc_normal_(m.weight, std=.02)
        if isinstance(m, nn.Linear) and m.bias is not None:
            torch.nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.LayerNorm):
        torch.nn.init.constant_(m.bias, 0)
        torch.nn.init.constant_(m.weight, 1.0)

# %% ../nbs/A. Neural modules.ipynb 4
def sinusoids(length, channels, max_timescale=10000):
    """Returns sinusoids for positional embedding"""
    assert channels % 2 == 0
    log_timescale_increment = np.log(max_timescale) / (channels // 2 - 1)
    inv_timescales = torch.exp(-log_timescale_increment * torch.arange(channels // 2))
    scaled_time = torch.arange(length)[:, np.newaxis] * inv_timescales[np.newaxis, :]
    return torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim=1)

# %% ../nbs/A. Neural modules.ipynb 5
class MultiHeadAttention(nn.Module):
    def __init__(self, n_state: int, n_head: int, qk_scale: float = 1):
        super().__init__()
        self.n_head = n_head
        self.sqrt_qk_scale = math.sqrt(qk_scale)
        self.query = QueryHead(n_state, n_state)
        self.key = nn.Linear(n_state, n_state, bias=False)
        self.value = nn.Linear(n_state, n_state)
        self.out = nn.Linear(n_state, n_state)

    def forward(
        self,
        x: Tensor,
        xa: Optional[Tensor] = None,
        causal = False,
        kv_cache: Optional[dict] = None,
    ):
        q = self.query(x)

        if kv_cache is None or xa is None or self.key not in kv_cache:
            # hooks, if installed (i.e. kv_cache is not None), will prepend the cached kv tensors;
            # otherwise, perform key/value projections for self- or cross-attention as usual.
            k = self.key(x if xa is None else xa)
            v = self.value(x if xa is None else xa)
        else:
            # for cross-attention, calculate keys and values once and reuse in subsequent calls.
            k = kv_cache[self.key]
            v = kv_cache[self.value]

        if self.sqrt_qk_scale != 1:
            q *= self.sqrt_qk_scale
            k *= self.sqrt_qk_scale

        wv, qk = self.qkv_attention_pth20(q, k, v, causal)
#         wv, qk = self.qkv_attention_xformers(q, k, v, causal)
        
        return self.out(wv), qk

    def qkv_attention_pth20(
        self, q: Tensor, k: Tensor, v: Tensor, causal = False
    ):
        n_batch, n_ctx, n_state = q.shape
        q = q.view(*q.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)
        k = k.view(*k.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)
        v = v.view(*v.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)

        # modified for better performance under PyTorch 2.0
        wv = F.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=0, is_causal=causal)

        # previously we've returned q@k which we don't have now
        # since it's not actually used anywhere else, let's just keep two return values for compatibility
        return wv.permute(0, 2, 1, 3).flatten(start_dim=2), None

    def qkv_attention_xformers(
        self, q: Tensor, k: Tensor, v: Tensor, causal = False
    ):
        n_batch, n_ctx, n_state = q.shape
        q = q.view(*q.shape[:2], self.n_head, -1)
        k = k.view(*k.shape[:2], self.n_head, -1)
        v = v.view(*v.shape[:2], self.n_head, -1)
        
        bias = xops.LowerTriangularMask() if causal else None
        wv = xops.memory_efficient_attention(q,k,v, attn_bias=bias)

        # previously we've returned q@k which we don't have now
        # since it's not actually used anywhere else, let's just keep two return values for compatibility
        return wv.flatten(start_dim=2), None

# %% ../nbs/A. Neural modules.ipynb 6
class ResidualAttentionBlock(nn.Module):
    def __init__(self, n_state: int, n_head: int, cross_attention: bool = False,
                 qk_scale: float = 1, ffn_mult: int = 4):
        super().__init__()

        self.attn = MultiHeadAttention(n_state, n_head, qk_scale=qk_scale)
        self.attn_ln = LayerNorm(n_state)

        self.cross_attn = (
            MultiHeadAttention(n_state, n_head, qk_scale=qk_scale) if cross_attention else None
        )
        self.cross_attn_ln = LayerNorm(n_state) if cross_attention else None

        n_mlp = n_state * ffn_mult
        self.mlp = nn.Sequential(
            nn.Linear(n_state, n_mlp), nn.GELU(), nn.Linear(n_mlp, n_state)
        )
        self.mlp_ln = LayerNorm(n_state)
        
    def forward(
        self,
        x: Tensor,
        xa: Optional[Tensor] = None,
        causal = False,
        kv_cache: Optional[dict] = None,
    ):
        x = x + self.attn(self.attn_ln(x), causal=causal, kv_cache=kv_cache)[0]
        if self.cross_attn:
            x = x + self.cross_attn(self.cross_attn_ln(x), xa, kv_cache=kv_cache)[0]
        x = x + self.mlp(self.mlp_ln(x))
        return x

# %% ../nbs/A. Neural modules.ipynb 7
class Encoder(nn.Module):
    def __init__(self, depth=6, width=384, n_head=6, length=1500, codes=1024, qk_scale=1, pos_embs=None):
        super().__init__()
    
        self.embedding = nn.Embedding(codes, width)

        if pos_embs is None: pos_embs = sinusoids(length, width)
        self.register_buffer("positional_embedding", pos_embs)

        self.layers = nn.Sequential(*[
            ResidualAttentionBlock(width, n_head, qk_scale=qk_scale) for _ in range(depth)
        ])

        self.ln_post = LayerNorm(width)
        
        self.apply(init_transformer)
        
    def forward(self, Stoks):
        xin = self.embedding(Stoks)
        
        assert xin.shape[1:] == self.positional_embedding.shape, "incorrect semantic token shape"
        xin = (xin + self.positional_embedding).to(xin.dtype)

        return self.ln_post(self.layers(xin))

# %% ../nbs/A. Neural modules.ipynb 8
class Decoder(nn.Module):
    def __init__(self, depth=6, width=384, n_head=6, length=1500, codes=1024, qk_scale=1, pos_embs=None):
        super().__init__()
        self.length = length
        self.codes = codes
    
        # embed semantic tokens
        self.embedding = nn.Embedding(codes+1, width)
        if pos_embs is None: pos_embs = sinusoids(length, width)
        self.register_buffer("positional_embedding", pos_embs)
        
        self.layers = nn.ModuleList([
            ResidualAttentionBlock(width, n_head, qk_scale=qk_scale, cross_attention=True) for _ in range(depth)
        ])
        self.ln_post = LayerNorm(width)
        
        self.apply(init_transformer)
        
    def forward(self, Stoks, xenc):
        sot = self.embedding(torch.tensor([self.codes]).cuda()).repeat(Stoks.shape[0],1,1)
        if Stoks.shape[-1] > 0:
            if Stoks.shape[-1] >= self.length:
                Stoks = Stoks[:,:-1]
            Sembs = self.embedding(Stoks)
            Sembs = torch.cat([sot, Sembs], dim=-2)
        else:
            Sembs = sot

        xin = (Sembs + self.positional_embedding[:Sembs.shape[1]]).to(xenc.dtype)
    
        x = xin
        for l in self.layers: x = l(x, xenc, causal=True)
        
        x = self.ln_post(x)
        
        logits = (x @ self.embedding.weight.to(x.dtype).T).float()
        return logits

# %% ../nbs/A. Neural modules.ipynb 9
class SumDecoder(nn.Module):
    def __init__(self, depth=6, width=384, n_head=6, length=9000, codes=1024, qk_scale=1, pos_embs=None):
        super().__init__()
        self.length = length
        self.codes = codes
    
        # embed semantic tokens
        self.embedding = nn.Embedding(codes+1, width)
        if pos_embs is None: pos_embs = sinusoids(length, width)
        self.register_buffer("positional_embedding", pos_embs)
        
        # before adding the encoder features
        self.layers = nn.ModuleList([
            ResidualAttentionBlock(width, n_head, qk_scale=qk_scale) for _ in range(math.floor(depth/2))
        ])

        # after adding the encoder features
        self.layers2 = nn.ModuleList([
            ResidualAttentionBlock(width, n_head, qk_scale=qk_scale) for _ in range(math.ceil(depth/2))
        ])

        self.ln_post = LayerNorm(width)
        
        self.apply(init_transformer)
        
    def forward(self, toks, xenc):
        sot = self.embedding(torch.tensor([self.codes]).cuda()).repeat(toks.shape[0],1,1)
        if toks.shape[-1] > 0:
            if toks.shape[-1] >= self.length:
                toks = toks[:,:-1]
            embs = self.embedding(toks)
            embs = torch.cat([sot, embs], dim=-2)
        else:
            embs = sot

        xin = (embs + self.positional_embedding[:embs.shape[1]]).to(xenc.dtype)
    
        x = xin

        for l in self.layers: x = l(x, causal=True)
        
        x += xenc.repeat_interleave(self.length // xenc.shape[-2], dim=-2)[:,:embs.shape[1]]

        for l in self.layers2: x = l(x, causal=True)
        
        x = self.ln_post(x)
        
        logits = (x @ self.embedding.weight.to(x.dtype).T).float()
        return logits
