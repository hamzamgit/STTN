# -*- coding: utf-8 -*-
"""
Created on Mon Sep 28 10:28:06 2020

@author: wb
"""

import torch
import torch.nn as nn
from GCN_models import GCN
from One_hot_encoder import One_hot_encoder


class SSelfAttention(nn.Module):
    def __init__(self, embed_size, heads):
        super(SSelfAttention, self).__init__()
        self.embed_size = embed_size
        self.heads = heads
        self.head_dim = embed_size // heads

        assert (
                self.head_dim * heads == embed_size
        ), "Embedding size needs to be divisible by heads"

        self.values = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.keys = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.queries = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.fc_out = nn.Linear(heads * self.head_dim, embed_size)

    def forward(self, values, keys, query):
        N, T, C = query.shape

        # Split the embedding into self.heads different pieces
        values = values.reshape(N, T, self.heads, self.head_dim)  # embed_size维拆成 heads×head_dim
        keys = keys.reshape(N, T, self.heads, self.head_dim)
        query = query.reshape(N, T, self.heads, self.head_dim)

        values = self.values(values)  # (N, T, heads, head_dim)
        keys = self.keys(keys)  # (N, T, heads, head_dim)
        queries = self.queries(query)  # (N, T, heads, heads_dim)

        # Einsum does matrix mult. for query*keys for each training example
        # with every other training example, don't be confused by einsum
        # it's just how I like doing matrix multiplication & bmm

        energy = torch.einsum("qthd,kthd->qkth", [queries, keys])  # 空间self-attention
        # queries shape: (N, T, heads, heads_dim),
        # keys shape: (N, T, heads, heads_dim)
        # energy: (N, N, T, heads)

        # Normalize energy values similarly to seq2seq + attention
        # so that they sum to 1. Also divide by scaling factor for
        # better stability
        attention = torch.softmax(energy / (self.embed_size ** (1 / 2)), dim=1)  # 在K维做softmax，和为1
        # attention shape: (N, N, T, heads)

        out = torch.einsum("qkth,kthd->qthd", [attention, values]).reshape(
            N, T, self.heads * self.head_dim
        )
        # attention shape: (N, N, T, heads)
        # values shape: (N, T, heads, heads_dim)
        # out after matrix multiply: (N, T, heads, head_dim), then
        # we reshape and flatten the last two dimensions.

        out = self.fc_out(out)
        # Linear layer doesn't modify the shape, final shape will be
        # (N, T, embed_size)

        return out


class TSelfAttention(nn.Module):
    def __init__(self, embed_size, heads):
        super(TSelfAttention, self).__init__()
        self.embed_size = embed_size
        self.heads = heads
        self.head_dim = embed_size // heads

        assert (
                self.head_dim * heads == embed_size
        ), "Embedding size needs to be divisible by heads"

        self.values = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.keys = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.queries = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.fc_out = nn.Linear(heads * self.head_dim, embed_size)

    def forward(self, values, keys, query):
        N, T, C = query.shape

        # Split the embedding into self.heads different pieces
        values = values.reshape(N, T, self.heads, self.head_dim)  # embed_size维拆成 heads×head_dim
        keys = keys.reshape(N, T, self.heads, self.head_dim)
        query = query.reshape(N, T, self.heads, self.head_dim)

        values = self.values(values)  # (N, T, heads, head_dim)
        keys = self.keys(keys)  # (N, T, heads, head_dim)
        queries = self.queries(query)  # (N, T, heads, heads_dim)

        # Einsum does matrix mult. for query*keys for each training example
        # with every other training example, don't be confused by einsum
        # it's just how I like doing matrix multiplication & bmm
        energy = torch.einsum("nqhd,nkhd->nqkh", [queries, keys])  # 时间self-attention
        # queries shape: (N, T, heads, heads_dim),
        # keys shape: (N, T, heads, heads_dim)
        # energy: (N, T, T, heads)

        # Normalize energy values similarly to seq2seq + attention
        # so that they sum to 1. Also divide by scaling factor for
        # better stability
        attention = torch.softmax(energy / (self.embed_size ** (1 / 2)), dim=2)  # 在K维做softmax，和为1
        # attention shape: (N, query_len, key_len, heads)

        out = torch.einsum("nqkh,nkhd->nqhd", [attention, values]).reshape(
            N, T, self.heads * self.head_dim
        )
        # attention shape: (N, T, T, heads)
        # values shape: (N, T, heads, heads_dim)
        # out after matrix multiply: (N, T, heads, head_dim), then
        # we reshape and flatten the last two dimensions.

        out = self.fc_out(out)
        # Linear layer doesn't modify the shape, final shape will be
        # (N, T, embed_size)

        return out


class STransformer(nn.Module):
    def __init__(self, embed_size, heads, adj, dropout, forward_expansion):
        super(STransformer, self).__init__()
        # Spatial Embedding
        self.adj = adj
        self.D_S = nn.Parameter(adj)
        self.embed_liner = nn.Linear(adj.shape[0], embed_size)

        self.attention = SSelfAttention(embed_size, heads)
        self.norm1 = nn.LayerNorm(embed_size)
        self.norm2 = nn.LayerNorm(embed_size)

        self.feed_forward = nn.Sequential(
            nn.Linear(embed_size, forward_expansion * embed_size),
            nn.ReLU(),
            nn.Linear(forward_expansion * embed_size, embed_size),
        )

        # 调用GCN
        self.gcn = GCN(embed_size, embed_size * 2, embed_size, dropout)
        self.norm_adj = nn.InstanceNorm2d(1)  # 对邻接矩阵归一化

        self.dropout = nn.Dropout(dropout)
        self.fs = nn.Linear(embed_size, embed_size)
        self.fg = nn.Linear(embed_size, embed_size)

    def forward(self, value, key, query):
        # Spatial Embedding 部分
        N, T, C = query.shape
        D_S = self.embed_liner(self.D_S)
        D_S = D_S.expand(T, N, C)
        D_S = D_S.permute(1, 0, 2)

        # GCN 部分
        X_G = torch.Tensor(query.shape[0], 0, query.shape[2])
        self.adj = self.adj.unsqueeze(0).unsqueeze(0)
        self.adj = self.norm_adj(self.adj)
        self.adj = self.adj.squeeze(0).squeeze(0)

        for t in range(query.shape[1]):
            o = self.gcn(query[:, t, :], self.adj)
            o = o.unsqueeze(1)  # shape [N, 1, C]
            X_G = torch.cat((X_G, o), dim=1)

        # Spatial Transformer 部分
        query = query + D_S
        attention = self.attention(value, key, query)
        # Add skip connection, run through normalization and finally dropout
        x = self.dropout(self.norm1(attention + query))
        forward = self.feed_forward(x)
        U_S = self.dropout(self.norm2(forward + x))

        # 融合 STransformer and GCN
        g = torch.sigmoid(self.fs(U_S) + self.fg(X_G))  # (7)
        out = g * U_S + (1 - g) * X_G  # (8)

        return out


class TTransformer(nn.Module):
    def __init__(self, embed_size, heads, time_num, dropout, forward_expansion):
        super(TTransformer, self).__init__()

        # Temporal embedding One hot
        self.time_num = time_num
        self.one_hot = One_hot_encoder(embed_size, time_num)  # temporal embedding选用one-hot方式 或者
        self.temporal_embedding = nn.Embedding(time_num, embed_size)  # temporal embedding选用nn.Embedding

        self.attention = TSelfAttention(embed_size, heads)
        self.norm1 = nn.LayerNorm(embed_size)
        self.norm2 = nn.LayerNorm(embed_size)

        self.feed_forward = nn.Sequential(
            nn.Linear(embed_size, forward_expansion * embed_size),
            nn.ReLU(),
            nn.Linear(forward_expansion * embed_size, embed_size),
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, value, key, query, t):
        N, T, C = query.shape

        D_T = self.one_hot(t, N, T)  # temporal embedding选用one-hot方式 或者
        D_T = self.temporal_embedding(torch.arange(0, T))  # temporal embedding选用nn.Embedding
        D_T = D_T.expand(N, T, C)

        # temporal embedding加到query。 原论文采用concatenated
        query = query + D_T

        attention = self.attention(value, key, query)

        # Add skip connection, run through normalization and finally dropout
        x = self.dropout(self.norm1(attention + query))
        forward = self.feed_forward(x)
        out = self.dropout(self.norm2(forward + x))
        return out


class STTransformerBlock(nn.Module):
    def __init__(self, embed_size, heads, adj, time_num, dropout, forward_expansion):
        super(STTransformerBlock, self).__init__()
        self.STransformer = STransformer(embed_size, heads, adj, dropout, forward_expansion)
        self.TTransformer = TTransformer(embed_size, heads, time_num, dropout, forward_expansion)
        self.norm1 = nn.LayerNorm(embed_size)
        self.norm2 = nn.LayerNorm(embed_size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, value, key, query, t):
        # Add skip connection,run through normalization and finally dropout
        x1 = self.norm1(self.STransformer(value, key, query) + query)
        x2 = self.dropout(self.norm2(self.TTransformer(x1, x1, x1, t) + x1))
        return x2


class Encoder(nn.Module):
    # 堆叠多层 ST-Transformer Block
    def __init__(
            self,
            embed_size,
            num_layers,
            heads,
            adj,
            time_num,
            device,
            forward_expansion,
            dropout,
    ):
        super(Encoder, self).__init__()
        self.embed_size = embed_size
        self.device = device
        self.layers = nn.ModuleList(
            [
                STTransformerBlock(
                    embed_size,
                    heads,
                    adj,
                    time_num,
                    dropout=dropout,
                    forward_expansion=forward_expansion
                )
                for _ in range(num_layers)
            ]
        )

        self.dropout = nn.Dropout(dropout)

    def forward(self, x, t):
        out = self.dropout(x)
        # In the Encoder the query, key, value are all the same.
        for layer in self.layers:
            out = layer(out, out, out, t)
        return out


class Transformer(nn.Module):
    def __init__(
            self,
            adj,
            embed_size=64,
            num_layers=3,
            heads=2,
            time_num=288,
            forward_expansion=4,
            dropout=0,
            device="cpu",
    ):
        super(Transformer, self).__init__()
        self.encoder = Encoder(
            embed_size,
            num_layers,
            heads,
            adj,
            time_num,
            device,
            forward_expansion,
            dropout,
        )
        self.device = device

    def forward(self, src, t):
        enc_src = self.encoder(src, t)
        return enc_src


class STTransformer(nn.Module):
    def __init__(
            self,
            adj,
            in_channels=1,
            embed_size=64,
            time_num=288,
            num_layers=3,
            T_dim=12,
            output_T_dim=3,
            heads=2,
    ):
        super(STTransformer, self).__init__()
        # 第一次卷积扩充通道数
        self.conv1 = nn.Conv2d(in_channels, embed_size, 1)
        self.Transformer = Transformer(
            adj,
            embed_size,
            num_layers,
            heads,
            time_num
        )

        # 缩小时间维度。  例：T_dim=12到output_T_dim=3，输入12维降到输出3维
        self.conv2 = nn.Conv2d(T_dim, output_T_dim, 1)
        # 缩小通道数，降到1维。
        self.conv3 = nn.Conv2d(embed_size, 1, 1)
        self.relu = nn.ReLU()

    def forward(self, x, t):
        # input x shape[ C, N, T] 
        # C:通道数量。  N:传感器数量。  T:时间数量

        x = x.unsqueeze(0)
        input_Transformer = self.conv1(x)
        input_Transformer = input_Transformer.squeeze(0)
        input_Transformer = input_Transformer.permute(1, 2, 0)

        # input_Transformer shape[N, T, C]
        output_Transformer = self.Transformer(input_Transformer, t)
        output_Transformer = output_Transformer.permute(1, 0, 2)
        # output_Transformer shape[T, N, C]

        output_Transformer = output_Transformer.unsqueeze(0)
        out = self.relu(self.conv2(output_Transformer))  # 等号左边 out shape: [1, output_T_dim, N, C]
        out = out.permute(0, 3, 2, 1)  # 等号左边 out shape: [1, C, N, output_T_dim]
        out = self.conv3(out)  # 等号左边 out shape: [1, 1, N, output_T_dim]
        out = out.squeeze(0).squeeze(0)

        return out
        # return out shape: [N, output_dim]
