from share import *
import config

import cv2
import einops
import gradio as gr
import numpy as np
import torch
import torchvision
import random

from pytorch_lightning import seed_everything
from annotator.util import resize_image, HWC3
from annotator.canny import CannyDetector
from cldm.model import create_model, load_state_dict
from cldm.ddim_hacked import DDIMSampler

# Added
from glob import glob
import os
import argparse
from tqdm import tqdm
import imageio
from pathlib import Path
import imageio


class TextToVideoCanny():
    def __init__(self, pos=None, neg=None, ddim_steps=20, guess_mode=False, strength=1., scale=9., seed=42, eta=0., low=100, high=200, num_samples=8, res=(512, 512), use_cf_attn=True, use_motion_field=True):
        self.apply_canny = CannyDetector()
        self.model = create_model('./models/cldm_v15.yaml').cpu() if use_cf_attn else create_model('./models/cldm_v15_no_cf_attn.yaml').cpu()
        self.model.load_state_dict(load_state_dict('./models/control/control_sd15_canny.pth', location='cuda'))
        self.model = self.model.cuda()
        self.ddim_sampler = DDIMSampler(self.model)

        self.pos_prompt = pos if pos else 'best quality, extremely detailed'
        self.neg_prompt = neg if neg else 'longbody, lowres, bad anatomy, bad hands, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality'
        self.ddim_steps = ddim_steps
        self.guess_mode = guess_mode
        self.strength = strength
        self.guidance_scale = scale
        self.seed = seed
        self.eta = eta
        self.low = low
        self.high = high
        self.n = num_samples
        self.res = res
        self.use_cf_attn = use_cf_attn
        self.use_motion_field = use_motion_field


    def video_path_to_image_list(self, input_video_path):
        is_canny = False

        vidcap = cv2.VideoCapture(input_video_path)
        success, image = vidcap.read()

        _, cnt = np.unique(image, return_counts=True)
        if cnt[0]/cnt.sum() > 0.2:
            is_canny = True
    
        image_list = []
        while success:
            image_list.append(image[:,:,::-1])
            success, image = vidcap.read()

        return image_list, is_canny


    def pre_process(self, input_image, image_resolution, low_threshold, high_threshold):
        img = cv2.resize(HWC3(input_image), image_resolution)
        H, W, C = img.shape

        detected_map = self.apply_canny(img, low_threshold, high_threshold)
        detected_map = HWC3(detected_map)

        control = torch.from_numpy(detected_map.copy()).float().cuda() / 255.0
        control = control.unsqueeze(0)

        return control, detected_map


    def inference(self, prompt, motion_path='__assets__/canny_videos_correct/deer_orig.mp4'):
        list_of_input_image, _ = self.video_path_to_image_list(motion_path)

        seed_everything(self.seed)
        H, W = self.res
        shape = (4, H // 8, W // 8)

        list_of_input_image = list_of_input_image[:(len(list_of_input_image)//self.n)*self.n]
        chunks = [list_of_input_image[x:x+8] for x in range(0, len(list_of_input_image), 8)]
        
        x_T = None
        if self.use_motion_field:
            x_T = torch.randn(shape).cuda()
            x_T = x_T.unsqueeze(0).repeat(self.n, 1, 1, 1)

        results = []

        for chunk in chunks:
            with torch.no_grad():
                controls = []
                detected_maps = []
                for input_image in chunk:
                    control, detected_map = self.pre_process(input_image, self.res, self.low, self.high)
                    controls.append(control)
                    detected_maps.append(detected_map)
                control = torch.cat(controls, dim=0)
                control = einops.rearrange(control, 'b h w c -> b c h w').clone()

                cond = {"c_concat": [control], "c_crossattn": [self.model.get_learned_conditioning([prompt + ', ' + self.pos_prompt] * self.n)]}
                un_cond = {"c_concat": None if self.guess_mode else [control], "c_crossattn": [self.model.get_learned_conditioning([self.neg_prompt] * self.n)]}

                self.model.control_scales = [self.strength * (0.825 ** float(12 - i)) for i in range(13)] if self.guess_mode else ([self.strength] * 13)  # Magic number. IDK why. Perhaps because 0.825**12<0.01 but 0.826**12>0.01
                samples, intermediates = self.ddim_sampler.sample(self.ddim_steps, self.n,
                    shape, cond, verbose=False, eta=self.eta,
                    unconditional_guidance_scale=self.guidance_scale,
                    unconditional_conditioning=un_cond, x_T=x_T)

                x_samples = self.model.decode_first_stage(samples)
                x_samples = (einops.rearrange(x_samples, 'b c h w -> b h w c') * 127.5 + 127.5).cpu().numpy().clip(0, 255).astype(np.uint8)

                chunk_result = [x_samples[i] for i in range(self.n)]
                results += chunk_result

        return results

