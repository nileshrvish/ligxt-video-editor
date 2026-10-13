"""RIFE v4.26 IFNet (inference only), from Practical-RIFE by hzwer (MIT License,
https://github.com/hzwer/Practical-RIFE; model files "under the same MIT license as this project").
Vendored so the downloaded model archive's Python files are never executed: only its weights
(flownet.pkl) are loaded, with torch.load(weights_only=True).
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

_grid: dict = {}


def warp(ten_input, ten_flow):
    """Backward warp (model/warplayer.py)."""
    k = (str(ten_flow.device), str(ten_flow.size()), ten_flow.dtype)
    if k not in _grid:
        n, _, h, w = ten_flow.shape
        hor = torch.linspace(-1.0, 1.0, w, device=ten_flow.device, dtype=ten_flow.dtype).view(1, 1, 1, w).expand(n, -1, h, -1)
        ver = torch.linspace(-1.0, 1.0, h, device=ten_flow.device, dtype=ten_flow.dtype).view(1, 1, h, 1).expand(n, -1, -1, w)
        _grid[k] = torch.cat([hor, ver], 1)
    ten_flow = torch.cat([ten_flow[:, 0:1] / ((ten_input.shape[3] - 1.0) / 2.0), ten_flow[:, 1:2] / ((ten_input.shape[2] - 1.0) / 2.0)], 1)
    g = (_grid[k] + ten_flow).permute(0, 2, 3, 1)
    return F.grid_sample(input=ten_input, grid=g.to(ten_input.dtype), mode="bilinear", padding_mode="border", align_corners=True)


def conv(i, o, k=3, s=1, p=1, d=1):
    return nn.Sequential(nn.Conv2d(i, o, kernel_size=k, stride=s, padding=p, dilation=d, bias=True), nn.LeakyReLU(0.2, True))


class Head(nn.Module):
    def __init__(self):
        super().__init__()
        self.cnn0 = nn.Conv2d(3, 16, 3, 2, 1)
        self.cnn1 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn2 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn3 = nn.ConvTranspose2d(16, 4, 4, 2, 1)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x):
        x = self.relu(self.cnn0(x))
        x = self.relu(self.cnn1(x))
        x = self.relu(self.cnn2(x))
        return self.cnn3(x)


class ResConv(nn.Module):
    def __init__(self, c, dilation=1):
        super().__init__()
        self.conv = nn.Conv2d(c, c, 3, 1, dilation, dilation=dilation, groups=1)
        self.beta = nn.Parameter(torch.ones((1, c, 1, 1)), requires_grad=True)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x):
        return self.relu(self.conv(x) * self.beta + x)


class IFBlock(nn.Module):
    def __init__(self, in_planes, c=64):
        super().__init__()
        self.conv0 = nn.Sequential(conv(in_planes, c // 2, 3, 2, 1), conv(c // 2, c, 3, 2, 1))
        self.convblock = nn.Sequential(*[ResConv(c) for _ in range(8)])
        self.lastconv = nn.Sequential(nn.ConvTranspose2d(c, 4 * 13, 4, 2, 1), nn.PixelShuffle(2))

    def forward(self, x, flow=None, scale=1):
        x = F.interpolate(x, scale_factor=1.0 / scale, mode="bilinear", align_corners=False)
        if flow is not None:
            flow = F.interpolate(flow, scale_factor=1.0 / scale, mode="bilinear", align_corners=False) * 1.0 / scale
            x = torch.cat((x, flow), 1)
        feat = self.convblock(self.conv0(x))
        tmp = F.interpolate(self.lastconv(feat), scale_factor=scale, mode="bilinear", align_corners=False)
        return tmp[:, :4] * scale, tmp[:, 4:5], tmp[:, 5:]


class IFNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.block0 = IFBlock(7 + 8, c=192)
        self.block1 = IFBlock(8 + 4 + 8 + 8, c=128)
        self.block2 = IFBlock(8 + 4 + 8 + 8, c=96)
        self.block3 = IFBlock(8 + 4 + 8 + 8, c=64)
        self.block4 = IFBlock(8 + 4 + 8 + 8, c=32)
        self.encode = Head()

    def forward(self, img0, img1, timestep, scale: float = 1.0):
        """img0/img1: (B,3,H,W) in 0..1 (H, W multiples of 64/scale); timestep: (B,1,1,1).
        Returns (frame at timestep, flow (B,4,H,W) to img0 and img1)."""
        scale_list = [16 / scale, 8 / scale, 4 / scale, 2 / scale, 1 / scale]
        ts = timestep.expand(-1, 1, img0.shape[2], img0.shape[3]).to(img0.dtype)
        f0, f1 = self.encode(img0), self.encode(img1)
        flow = mask = feat = None
        w0, w1 = img0, img1
        for i, blk in enumerate((self.block0, self.block1, self.block2, self.block3, self.block4)):
            if flow is None:
                flow, mask, feat = blk(torch.cat((img0, img1, f0, f1, ts), 1), None, scale=scale_list[i])
            else:
                wf0, wf1 = warp(f0, flow[:, :2]), warp(f1, flow[:, 2:4])
                fd, mask, feat = blk(torch.cat((w0, w1, wf0, wf1, ts, mask, feat), 1), flow, scale=scale_list[i])
                flow = flow + fd
            w0, w1 = warp(img0, flow[:, :2]), warp(img1, flow[:, 2:4])
        m = torch.sigmoid(mask)
        return w0 * m + w1 * (1 - m), flow
