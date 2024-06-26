# 2022.06.28-Changed for building CMT
#            Huawei Technologies Co., Ltd. <foss@huawei.com>
# Author: Jianyuan Guo (jyguo@pku.edu.cn)

#change the conv in CMT block
import math
import logging
from functools import partial
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.helpers import load_pretrained
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.resnet import resnet26d, resnet50d
from timm.models.registry import register_model
from timm.models import load_checkpoint

_logger = logging.getLogger(__name__)


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'patch_embed.proj', 'classifier': 'head',
        **kwargs
    }

class CrossAttention(nn.Module):
    def __init__(self, dim1,dim2, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.dim = dim
        self.dim1 = dim1
        self.dim2 = dim2
        self.scale = qk_scale or head_dim ** -0.5

        self.q1 = nn.Linear(dim1, dim, bias=qkv_bias)
        self.proj = nn.Linear(dim, dim1)

        self.k2 = nn.Linear(dim2, dim, bias=qkv_bias)
        self.v2 = nn.Linear(dim2, dim, bias=qkv_bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, fea, depth_fea):
        _, N1, _ = fea.shape
        B, N, _ = depth_fea.shape
        C = self.dim
        q1 = self.q1(fea).reshape(B, N1, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        # q [B, nhead, N, C//nhead]

        k2 = self.k2(depth_fea).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        v2 = self.v2(depth_fea).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)

        attn = (q1 @ k2.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        fea = (attn @ v2).transpose(1, 2).reshape(B, N1, C)
        fea = self.proj(fea)
        fea = self.proj_drop(fea)

        return fea
    
    def flops(self,N1,N2):
        flops = 0
        #q
        flops += N1*self.dim1*self.dim
        #kv
        flops += N2*self.dim2*self.dim*2
        #qk
        flops += self.num_heads*N1*self.dim//self.num_heads*N2
        #att v
        flops += self.num_heads*N1*self.dim//self.num_heads*N2
        #proj
        flops += N1*self.dim*self.dim1
        return flops
        
class BasicConv2d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1):
        super(BasicConv2d, self).__init__()
        self.conv = nn.Conv2d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x

class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, 
                 attn_drop=0., proj_drop=0., qk_ratio=1, sr_ratio=1):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.qk_dim = dim // qk_ratio

        self.q = nn.Linear(dim, self.qk_dim, bias=qkv_bias)
        self.k = nn.Linear(dim, self.qk_dim, bias=qkv_bias)
        self.v = nn.Linear(dim, dim, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        
        self.sr_ratio = sr_ratio
        # Exactly same as PVTv1
        if self.sr_ratio > 1:
            self.sr = nn.Sequential(
                nn.Conv2d(dim, dim, kernel_size=sr_ratio, stride=sr_ratio, groups=dim, bias=True),
                nn.BatchNorm2d(dim, eps=1e-5),
            )

    def forward(self, x, H, W, relative_pos):
        B, N, C = x.shape
        q = self.q(x).reshape(B, N, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)
        
        if self.sr_ratio > 1:
            x_ = x.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = self.sr(x_).reshape(B, C, -1).permute(0, 2, 1)
            k = self.k(x_).reshape(B, -1, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)
            v = self.v(x_).reshape(B, -1, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        else:
            k = self.k(x).reshape(B, N, self.num_heads, self.qk_dim // self.num_heads).permute(0, 2, 1, 3)
            v = self.v(x).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        attn = (q @ k.transpose(-2, -1)) * self.scale 
        attn = attn + relative_pos
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, qk_ratio=1, sr_ratio=1):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, 
            attn_drop=attn_drop, proj_drop=drop, qk_ratio=qk_ratio, sr_ratio=sr_ratio)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        # self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.proj = nn.Conv2d(dim, dim, 3, 1, 1, groups=dim)
        
    def forward(self, x, H, W, relative_pos):
        B, N, C = x.shape
        x = x + self.drop_path(self.attn(self.norm1(x), H, W, relative_pos))
        # x = x + self.drop_path(self.mlp(self.norm2(x), H, W))
        cnn_feat = x.permute(0, 2, 1).reshape(B, C, H, W)
        x = self.proj(cnn_feat) + cnn_feat
        x = x.flatten(2).permute(0, 2, 1)
        return x

# LPU unit 加到 patch embed中fold 后面
class PatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768, kernel_size=3, stride=2, padding=1, fuse=True):
        super().__init__()
        ms = img_size
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] * patch_size[1]) * (img_size[0] * patch_size[0])
        
        
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches

        self.project = nn.Linear(in_chans, in_chans * kernel_size * kernel_size)
        self.upsample = nn.Fold(output_size=(ms*stride, ms*stride) , kernel_size=(kernel_size, kernel_size), stride=(stride, stride), padding=(padding, padding))
        self.norm = nn.LayerNorm(in_chans)
        self.norm2 = nn.LayerNorm(embed_dim)
        self.norm3 = nn.LayerNorm(embed_dim)
        # self.lpu = nn.Conv2d(in_chans, in_chans, 3, 1, 1, groups = in_chans)
        self.lpu = nn.Conv2d(in_chans, in_chans, 3, 1, 1)
        self.fuse = fuse
        if self.fuse:
            self.concatFuse = nn.Sequential(
                nn.Linear(in_chans+embed_dim, in_chans),
                nn.GELU(),
                nn.Linear(in_chans, embed_dim),
            )
            self.interact = CrossAttention(dim1 = embed_dim,dim2 = in_chans,dim = embed_dim,num_heads=4 ,qkv_bias=False,qk_scale=None,attn_drop=0.,proj_drop=0.)
        else:
            self.concatFuse = nn.Sequential(
                nn.Linear(in_chans, in_chans),
                nn.GELU(),
                nn.Linear(in_chans, embed_dim),
            )
            
    def forward(self, x, enc_fea):
        B, C, H, W = x.shape
        # FIXME look at relaxing size constraints
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.project(x.flatten(2).transpose(1, 2))
        x = self.upsample(x.transpose(1,2))
        x = self.lpu(x) + x
        x = x.flatten(2).transpose(1, 2)
        x = self.norm(x)
        x0 = x.clone()
        H, W = H * self.patch_size[0], W * self.patch_size[1]
        # print(enc_fea.shape, x.shape)
        if self.fuse:
            x = self.norm2(self.concatFuse(torch.cat([x, enc_fea], dim=2)))
            x = x + self.interact(x, x0)
            x = self.norm3(x)
        else:
            x = self.norm2(self.concatFuse(x))
        return x, (H, W)
    
class DecoderC(nn.Module):
    def __init__(self, img_size=224, in_chans=3, num_classes=1000, embed_dims=[384,128,64,16,4], stem_channel=16, fc_dim=1280,
                 num_heads=[1,2,4,8], mlp_ratios=[3.6,3.6,3.6,3.6], qkv_bias=True, qk_scale=None, representation_size=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0., hybrid_backbone=None, norm_layer=None,
                 depths=[2,3,6,3], qk_ratio=1, sr_ratios=[8,4,2,1], dp=0.1):
        super().__init__()
        self.norm = nn.LayerNorm(embed_dims[0])
        self.mlp = nn.Sequential(
                    nn.Linear(embed_dims[0], embed_dims[0]),
                    nn.GELU(),
                    nn.Linear(embed_dims[0], embed_dims[1]),
                )
        self.img_size = img_size
        
        self.patch_embed_b = PatchEmbed(
            img_size=img_size//4, patch_size=4, in_chans=embed_dims[2], embed_dim=embed_dims[3], kernel_size=7, stride=4, padding=2, fuse=False)
        self.patch_embed_c = PatchEmbed(
            img_size=img_size//8, patch_size=2, in_chans=embed_dims[1], embed_dim=embed_dims[2], kernel_size=3, stride=2, padding=1, fuse=True)
        self.patch_embed_d = PatchEmbed(
            img_size=img_size//16, patch_size=2, in_chans=embed_dims[0], embed_dim=embed_dims[1], kernel_size=3, stride=2, padding=1, fuse=True)

        # self.relative_pos_a = nn.Parameter(torch.randn(
        #     num_heads[0], self.patch_embed_a.num_patches, self.patch_embed_a.num_patches//sr_ratios[0]//sr_ratios[0]))
        # self.relative_pos_b = nn.Parameter(torch.randn(
        #     num_heads[1], self.patch_embed_b.num_patches, self.patch_embed_b.num_patches//sr_ratios[1]//sr_ratios[1]))
        self.relative_pos_c = nn.Parameter(torch.randn(
            num_heads[2], self.patch_embed_c.num_patches, self.patch_embed_c.num_patches//sr_ratios[2]//sr_ratios[2]))
        self.relative_pos_d = nn.Parameter(torch.randn(
            num_heads[3], self.patch_embed_d.num_patches, self.patch_embed_d.num_patches//sr_ratios[3]//sr_ratios[3]))
        
        self.pre_1_16 = nn.Linear(embed_dims[1], 1)
        self.pre_1_8 = nn.Linear(embed_dims[1], 1)
        self.pre_1_4 = nn.Linear(embed_dims[2], 1)
        self.pre_1_1 = nn.Linear(embed_dims[3], 1)

        self.s1 = nn.Sigmoid()
        self.s2 = nn.Sigmoid()
        self.s3 = nn.Sigmoid()
        self.s4 = nn.Sigmoid()
        
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule
        cur = 0
        # self.blocks_a = nn.ModuleList([
        #     Block(
        #         dim=embed_dims[1], num_heads=num_heads[0], mlp_ratio=mlp_ratios[0], qkv_bias=qkv_bias,
        #         qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur+i],
        #         qk_ratio=qk_ratio, sr_ratio=sr_ratios[0])
        #     for i in range(depths[0])])
        cur += depths[0]
        # self.blocks_b = nn.ModuleList([
        #     Block(
        #         dim=embed_dims[3], num_heads=num_heads[1], mlp_ratio=mlp_ratios[1], qkv_bias=qkv_bias,
        #         qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur+i],
        #         qk_ratio=qk_ratio, sr_ratio=sr_ratios[1])
        #     for i in range(depths[1])])
        cur += depths[1]
        self.blocks_c = nn.ModuleList([
            Block(
                dim=embed_dims[2], num_heads=num_heads[2], mlp_ratio=mlp_ratios[2], qkv_bias=qkv_bias,
                qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur+i],
                qk_ratio=qk_ratio, sr_ratio=sr_ratios[2])
            for i in range(depths[2])])
        cur += depths[2]
        self.blocks_d = nn.ModuleList([
            Block(
                dim=embed_dims[1], num_heads=num_heads[3], mlp_ratio=mlp_ratios[3], qkv_bias=qkv_bias,
                qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[cur+i],
                qk_ratio=qk_ratio, sr_ratio=sr_ratios[3])
            for i in range(depths[3])])

        # Representation layer
        if representation_size:
            self.num_features = representation_size
            self.pre_logits = nn.Sequential(OrderedDict([
                ('fc', nn.Linear(embed_dim, representation_size)),
                ('act', nn.Tanh())
            ]))
        else:
            self.pre_logits = nn.Identity()

        # Classifier head

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.Conv2d):
            nn.init.kaiming_normal_(m.weight, mode='fan_out')
            if isinstance(m, nn.Conv2d) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
            
    def update_temperature(self):
        for m in self.modules():
            if isinstance(m, Attention):
                m.update_temperature()

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}


    def forward_features(self, x, x_1_8, x_1_4):
        B, N, C = x.shape
        
        saliency_fea_1_16 = self.mlp(self.norm(x))
        mask_1_16 = self.pre_1_16(saliency_fea_1_16)
        mask_1_16 = mask_1_16.transpose(1, 2).reshape(B, 1, self.img_size // 16, self.img_size // 16)
        
        x = x.transpose(1,2).reshape(B, C, 14, 14)
        x, (H, W) = self.patch_embed_d(x, x_1_8)
        for i, blk in enumerate(self.blocks_d):
            x = blk(x, H, W, self.relative_pos_d)
        
 #       x_1_8
        mask_1_8 = self.pre_1_8(x)
        mask_1_8 = mask_1_8.transpose(1, 2).reshape(B, 1, self.img_size // 8, self.img_size // 8)

        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        x, (H, W) = self.patch_embed_c(x, x_1_4)
        for i, blk in enumerate(self.blocks_c):
            x = blk(x, H, W, self.relative_pos_c)
            
        mask_1_4 = self.pre_1_4(x)
        mask_1_4 = mask_1_4.transpose(1, 2).reshape(B, 1, self.img_size // 4, self.img_size // 4)
        # x_1_16      
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        x, (H, W) = self.patch_embed_b(x, None)
        # for i, blk in enumerate(self.blocks_b):
        #     x = blk(x, H, W, self.relative_pos_b)
            
        mask_1_1 = self.pre_1_1(x)
        mask_1_1 = mask_1_1.transpose(1, 2).reshape(B, 1, self.img_size // 1, self.img_size // 1)
        # x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        # x, (H, W) = self.patch_embed_d(x)
        # for i, blk in enumerate(self.blocks_d):
        #     x = blk(x, H, W, self.relative_pos_d)
        return [mask_1_16, mask_1_8, mask_1_4, mask_1_1]

    def forward(self, x, x_1_8, x_1_4):
        [mask_1_16, mask_1_8, mask_1_4, mask_1_1] = self.forward_features(x, x_1_8, x_1_4)

        m_1 = self.s1(mask_1_1)
        m_4 = self.s2(mask_1_4)
        m_8 = self.s3(mask_1_8)
        m_16 = self.s4(mask_1_16)

        return [mask_1_16, mask_1_8, mask_1_4, mask_1_1, m_16, m_8, m_4, m_1]
