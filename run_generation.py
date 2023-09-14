import os
# Workaround for PyTorch spawning too many threads
os.environ['OMP_NUM_THREADS'] = '1'

import os.path
import pathlib
import argparse
import sys
import time
import math
import glob

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data

from rendering.mesh_template import MeshTemplate
from rendering.utils import qrot

from utils.fid import RepeatIterator, calculate_stats, calculate_frechet_distance, init_inception, forward_inception_batch
from utils.losses import GANLoss, loss_flat

from data.pseudo_dataset import PseudoDataset, PseudoDatasetForEvaluation
from data.definitions import class_indices, default_cache_directories

from models.gan import MultiScaleDiscriminator, Generator

try:
    from tqdm import tqdm
except ImportError:
    print('Warning: tqdm not found. Install it to see the progress bar.')
    def tqdm(x): return x

parser = argparse.ArgumentParser()

# Model settings
parser.add_argument('--texture_resolution', type=int, default=512)
parser.add_argument('--mesh_resolution', type=int, default=32)
parser.add_argument('--symmetric_g', type=bool, default=True)
parser.add_argument('--texture_only', action='store_true')
parser.add_argument('--conditional_class', action='store_true', help='condition the model on class labels')
parser.add_argument('--conditional_color', action='store_true', help='condition the model on colors')
parser.add_argument('--conditional_text', action='store_true', help='condition the model on captions')
parser.add_argument('--conditional_semantics', action='store_true', help='condition the model on semantics')
parser.add_argument('--predict_semantics', action='store_true', help='the model will predict semantic maps as well')
parser.add_argument('--num_parts', type=int, default=-1, help='number of semantic parts (-1 = autodetect)')
parser.add_argument('--norm_g', type=str, default='syncbatch', help='(syncbatch|batch|instance|none)')
parser.add_argument('--latent_dim', type=int, default=64, help='dimensionality of the random vector z')
parser.add_argument('--mesh_path', type=str, default='autodetect', help='path to the .obj mesh template')

parser.add_argument('--text_max_length', type=int, default=18)
parser.add_argument('--text_pretrained_encoder', type=str, default='cache/cub/text_encoder200.pth')
parser.add_argument('--text_train_encoder', action='store_true') # Disabled by default (unstable)
parser.add_argument('--text_attention', type=bool, default=True)
parser.add_argument('--text_embedding_dim', type=int, default=256)

# Training settings
parser.add_argument('--epochs', type=int, default=600, help='number of training epochs (mutually exclusive with --iters)')
parser.add_argument('--iters', type=int, default=-1, help='number of training iterations (mutually exclusive with --epochs)')
parser.add_argument('--norm_d', type=str, default='none', help='(instance|none)')
parser.add_argument('--mesh_regularization', type=float, default=0.0001, help='strength of the smoothness regularizer')
parser.add_argument('--lr_g', type=float, default=0.0001)
parser.add_argument('--lr_d', type=float, default=0.0004)
parser.add_argument('--d_steps_per_g', type=int, default=2)
parser.add_argument('--g_running_average_alpha', type=float, default=0.999)
parser.add_argument('--lr_decay_after', type=int, default=100000) # Set to a very large value to disable
parser.add_argument('--loss', type=str, default='hinge', help='(hinge|ls|original)')
parser.add_argument('--mask_output', type=bool, default=True)
parser.add_argument('--num_discriminators', type=int, default=-1) # -1 = autodetect

# Session settings
parser.add_argument('--name', type=str, required=True, help='name of the experiment')
parser.add_argument('--dataset', type=str, required=True, help='dataset to use')
parser.add_argument('--mode', type=str, default='autodetect', help='single or multiple templates (autodetect|singletpl|multitpl)')
parser.add_argument('--checkpoint_freq', type=int, default=20, help='save checkpoint every N epochs')
parser.add_argument('--save_freq', type=int, default=5, help='save latest checkpoint every N epochs')
parser.add_argument('--evaluate_freq', type=int, default=20, help='evaluate FID every N epochs')
parser.add_argument('--tensorboard', action='store_true')
parser.add_argument('--gpu_ids', type=str, default='0', help='comma-separated')
parser.add_argument('--continue_train', action='store_true', help='resume training from checkpoint')
parser.add_argument('--evaluate', action='store_true', help='evaluate FID, do not train')
parser.add_argument('--export_sample', action='store_true', help='export image/mesh samples, do not train')
parser.add_argument('--how_many', type=int, default=-1, help='how many samples to export with --export_sample, defaults to batch size')
parser.add_argument('--which_epoch', type=str, default='latest', help='(N|latest|best)') # Epoch from which to resume (or evaluate)
parser.add_argument('--batch_size', type=int, default=32)
parser.add_argument('--num_workers', type=int, default=4, help='number of data-loading threads')
parser.add_argument('--truncation_sigma', type=float, default=-1, help='-1 = autodetect; set to a large value to disable')
parser.add_argument('--filter_class', type=str, help='class to select in conditional model (evaluation only)')

args = parser.parse_args()
assert not (args.conditional_semantics and args.predict_semantics), 'This mode is unsupported.'

cache_dir = os.path.join('cache', args.dataset)
if not os.path.isdir(cache_dir):
    print('Cache directory does not exist!', cache_dir)
    sys.exit(1)

if args.export_sample:
    args.evaluate = True
    
assert args.mode in ['autodetect', 'singletpl', 'multitpl'], 'Invalid mode'
if args.mode == 'autodetect':
    if 'singletpl' in args.name:
        print('Autodetected single-template setting')
        args.mode = 'singletpl'
    elif 'multitpl' in args.name:
        print('Autodetected multi-template setting')
        args.mode = 'multitpl'
    else:
        print('Unable to autodetect setting (single-template or multi-template) from the experiment name. '
              'Please specify --mode manually.')
        sys.exit(1)

train_ds = PseudoDataset(args)
if args.conditional_semantics:
    args.num_parts = train_ds[0]['seg'].shape[0]
    print(f'Enabled semantic conditioning with {args.num_parts} object parts')

if args.predict_semantics:
    args.num_parts = train_ds[0]['seg_inv_rend'].shape[0]
    print(f'Enabled semantics prediction with {args.num_parts} object parts')
    
if args.iters != -1:
    print('Iterations specified:', args.iters)
    iters_per_epoch = int(len(train_ds) / args.batch_size)
    print('Iterations per epoch:', iters_per_epoch)
    args.epochs = int(args.iters / iters_per_epoch)
    args.evaluate_freq = int(args.epochs / 30)
    args.checkpoint_freq = args.evaluate_freq
    print(f'*** Training for {args.epochs} epochs, saving/evaluating every {args.evaluate_freq} epochs ***')



if args.mesh_path == 'autodetect':
    args.mesh_path = train_ds.suggest_mesh_template()
    print('Using autodetected mesh', args.mesh_path)
        
if args.num_discriminators == -1:
    # Autodetect
    args.num_discriminators = train_ds.suggest_num_discriminators()
        
if args.truncation_sigma < 0:
    # Autodetect
    args.truncation_sigma = train_ds.suggest_truncation_sigma()
    print(f'Using truncation sigma {args.truncation_sigma} for evaluation')
    
# A few safety checks...
if args.num_discriminators >= 3:
    assert args.texture_resolution >= 512

if args.conditional_color or args.conditional_text:
    print('--conditional_color or --conditional_text are unsupported!')
    sys.exit(1)
    
if args.filter_class is not None and (not args.evaluate or not args.conditional_class):
    print('--filter_class requires --conditional_class and --evaluate/--export_sample')
    sys.exit(1)
    
        
if args.norm_g == 'syncbatch':
    # Import library for synchronized batch normalization
    from sync_batchnorm import DataParallelWithCallback
    
if args.conditional_text:
    from utils.text_functions import RNN_Encoder
    
if args.tensorboard and not args.evaluate:
    import shutil
    from torch.utils.tensorboard import SummaryWriter
    import torchvision
    
gpu_ids = [int(x) for x in args.gpu_ids.split(',')]
print('Using {} GPUs: {}'.format(len(gpu_ids), gpu_ids))
torch.cuda.set_device(min(gpu_ids))
    
eval_ds = PseudoDatasetForEvaluation(train_ds)

if args.dataset == 'all':
    print('Balancing dataset.')
    sampler = torch.utils.data.WeightedRandomSampler(train_ds.image_weights, len(train_ds))
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=args.batch_size, num_workers=args.num_workers,
                                               pin_memory=False, drop_last=True, sampler=sampler)
else:
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=args.batch_size, num_workers=args.num_workers,
                                               pin_memory=False, drop_last=True, shuffle=True)

    
if args.evaluate and args.filter_class is not None:
    filter_class_idx = class_indices[args.filter_class]
    print('Using subset', filter_class_idx, args.filter_class)
    filter_indices, = torch.where(train_ds.classes[:, 0] == filter_class_idx)
    print('Filtering', len(filter_indices), 'images')
    eval_ds = torch.utils.data.Subset(eval_ds, filter_indices)
    # Override cache dir
    cache_dir = os.path.join('cache', default_cache_directories[args.filter_class])
    print('Overriding cache dir:', cache_dir)


eval_loader = torch.utils.data.DataLoader(eval_ds, batch_size=args.batch_size, num_workers=args.num_workers,
                                          pin_memory=False, shuffle=False)


if not args.texture_only:
    # Load libraries needed for differentiable rendering and FID evaluation
    from rendering.renderer import Renderer
    import scipy

    mesh_template = MeshTemplate(args.mesh_path, is_symmetric=args.symmetric_g)

    # For real-time FID evaluation
    if not args.export_sample:
        evaluation_res = 299 # Same as Inception input resolution
    else:
        evaluation_res = 1024 # For exporting images: higher resolution
    renderer = Renderer(evaluation_res, evaluation_res)
    renderer = nn.DataParallel(renderer, gpu_ids)

    if not args.export_sample:
        inception_model = nn.DataParallel(init_inception(), gpu_ids).cuda().eval()

        # Load precomputed statistics to speed up FID computation
        stats = np.load(os.path.join(cache_dir, f'precomputed_fid_{evaluation_res}x{evaluation_res}.npz'), allow_pickle=True)
        m_real_val = stats['stats_m']
        s_real_val = stats['stats_s'] + np.triu(stats['stats_s'].T, 1)
        n_images_val = min(stats['num_images'], 50000)
        print(f'Found statistics for FID computation (# images: {n_images_val})')
        assert stats['resolution'] == evaluation_res, 'Resolution does not match'
        stats = None
    


def evaluate_fid(writer, it, visualization_indices=None, fast=False):
    global m_real_val, s_real_val
    
    emb_arr_fake_combined = []
    emb_arr_fake_combined_seg = []
    emb_arr_fake_texture_only = []
    emb_arr_fake_mesh_only = []
    emb_arr_fake_seg_only = []
    
    # Grid for visualization
    if visualization_indices is not None:
        indices_to_render = visualization_indices.numpy()
        shuffle_idx = np.argsort(np.argsort(indices_to_render)) # To restore the original order
    else:
        if args.filter_class is None:
            indices_to_render = np.random.choice(len(train_ds), size=16, replace=False)
        else:
            indices_to_render = None
        shuffle_idx = None
        
    with torch.no_grad():
        generator_running_avg.eval()

        sample_real = []
        sample_fake = []
        sample_fake_texture_only = []
        sample_fake_mesh_only = []
        sample_text = [] # For models trained with captions
        sample_seg = []
        sample_seg_fake = []
        sample_tex_real = []
        sample_tex_fake = []
        sample_mesh_map_fake = []
        
        if args.evaluate:
            # Deterministic seed, but only in evaluation mode since we do not want to reset
            # the random state while we train the model (it would cripple the model).
            # Note that FID scores might still exhibit some variability depending on the batch size.
            torch.manual_seed(1234)
        
        num_passes = (n_images_val + len(eval_ds) - 1) // len(eval_ds) # Round up
        print(f'Evaluating FID on {n_images_val} images...')
        for data in tqdm(RepeatIterator(eval_loader, num_passes)):
            for k in ['texture', 'mesh', 'translation', 'scale', 'rotation']:
                if k in data:
                    data[k] = data[k].cuda()

            has_pseudogt = 'texture' in data and not fast

            if args.conditional_class:
                c = data['class'].cuda()
                caption = None
            elif args.conditional_text:
                c = None
                caption = tuple([x.cuda() for x in data['caption']])
            else:
                c, caption = None, None
                
            if args.conditional_semantics:
                seg = data['seg'].cuda()
                
            elif args.predict_semantics:
                seg = data['seg_inv_rend'].cuda()
            
            else:
                seg = None
            
            
            
            noise = torch.randn(data['idx'].shape[0], args.latent_dim)
            
            # Gaussian truncation trick
            sigma = args.truncation_sigma
            while (noise.abs() > sigma).any():
                # Rejection sampling
                mask = noise.abs() > sigma
                noise[mask] = torch.randn_like(noise[mask])

            noise = noise.cuda()
            
            if noise.shape[0] % len(gpu_ids) == 0:
                pred_tex, pred_mesh_map, pred_seg, attn_map = trainer('inference', None, None, C=c, caption=caption, seg=seg, noise=noise)
            else:
                # Batch dimension is not divisible by number of GPUs --> pad
                original_bsz = noise.shape[0]
                padding_bsz = len(gpu_ids) - (noise.shape[0] % len(gpu_ids))
                def pad_batch(batch):
                    return torch.cat((batch, torch.zeros((padding_bsz, *batch.shape[1:]),
                                                         dtype=batch.dtype).to(batch.device)), dim=0)
                    
                noise_pad = pad_batch(noise)
                if c is not None:
                    c_pad = pad_batch(c)
                else:
                    c_pad = None
                if caption is not None:
                    caption_pad = tuple([pad_batch(x) for x in caption])
                else:
                    caption_pad = None
                if seg is not None:
                    seg_pad = pad_batch(seg)
                else:
                    seg_pad = None

                pred_tex, pred_mesh_map, pred_seg, attn_map = trainer('inference', None, None, C=c_pad,
                                                            caption=caption_pad, seg=seg_pad, noise=noise_pad)
                
                # Unpad
                pred_tex = pred_tex[:original_bsz]
                pred_mesh_map = pred_mesh_map[:original_bsz]
                if attn_map is not None:
                    attn_map = attn_map[:original_bsz]
                if args.predict_semantics:
                    pred_seg = pred_seg[:original_bsz]

            def render_and_score(input_mesh_map, input_texture, output_array):
                vtx = mesh_template.get_vertex_positions(input_mesh_map)
                vtx = qrot(data['rotation'], data['scale'].unsqueeze(-1)*vtx) + data['translation'].unsqueeze(1)
                vtx = vtx * torch.Tensor([1, -1, -1]).to(vtx.device)

                image_pred, _ = mesh_template.forward_renderer(renderer, vtx, input_texture, len(gpu_ids))
                image_pred = image_pred.permute(0, 3, 1, 2)/2 + 0.5
                
                emb = forward_inception_batch(inception_model, image_pred)
                output_array.append(emb)
                return image_pred # Return images for visualization

            out_combined = render_and_score(pred_mesh_map, pred_tex, emb_arr_fake_combined)
            
            if args.filter_class is not None and indices_to_render is None:
                indices_to_render = data['idx'].cpu().numpy()[:16]
            mask, = np.where(np.isin(data['idx'].cpu().numpy(), indices_to_render))
            if len(mask) > 0:
                sample_fake.append(out_combined[mask].cpu())
                sample_mesh_map_fake.append(pred_mesh_map[mask].cpu())
                sample_tex_fake.append(pred_tex[mask].cpu())
                if has_pseudogt:
                    sample_real.append(data['image'][mask])
                    sample_tex_real.append(data['texture'][mask].cpu())
                if args.conditional_text:
                    sample_text.append(caption[0][mask].cpu())
                if args.conditional_semantics: 
                    sample_seg.append(seg[mask].cpu())
                #elif args.predict_semantics:
                #    sample_seg_fake.append(pred_seg[mask].cpu())
                #    raise NotImplemented
                    
                
            if has_pseudogt:
                out_combined = render_and_score(data['mesh'], pred_tex, emb_arr_fake_texture_only)
                if len(mask) > 0:
                    sample_fake_texture_only.append(out_combined[mask].cpu())
                out_combined = render_and_score(pred_mesh_map, data['texture'], emb_arr_fake_mesh_only)
                if len(mask) > 0:
                    sample_fake_mesh_only.append(out_combined[mask].cpu())
                
    emb_arr_fake_combined = np.concatenate(emb_arr_fake_combined, axis=0)
    if has_pseudogt:
        emb_arr_fake_texture_only = np.concatenate(emb_arr_fake_texture_only, axis=0)
        emb_arr_fake_mesh_only = np.concatenate(emb_arr_fake_mesh_only, axis=0)
        sample_real = torch.cat(sample_real, dim=0)
    sample_fake = torch.cat(sample_fake, dim=0)
    sample_mesh_map_fake = torch.cat(sample_mesh_map_fake, dim=0)
    sample_tex_fake = torch.cat(sample_tex_fake, dim=0)
    if has_pseudogt:
        sample_fake_texture_only = torch.cat(sample_fake_texture_only, dim=0)
        sample_fake_mesh_only = torch.cat(sample_fake_mesh_only, dim=0)
        sample_tex_real = torch.cat(sample_tex_real, dim=0)
    if args.conditional_text:
        sample_text = torch.cat(sample_text, dim=0)
    if args.conditional_semantics: 
        sample_seg = torch.cat(sample_seg, dim=0)
    #elif args.predict_semantics:
    #    sample_seg_fake = torch.cat(sample_seg_fake, dim=0)
    #    raise NotImplemented
        
    if shuffle_idx is not None:
        sample_fake = sample_fake[shuffle_idx]
        sample_mesh_map_fake = sample_mesh_map_fake[shuffle_idx]
        sample_tex_fake = sample_tex_fake[shuffle_idx]
        if has_pseudogt:
            sample_real = sample_real[shuffle_idx]
            sample_fake_texture_only = sample_fake_texture_only[shuffle_idx]
            sample_fake_mesh_only = sample_fake_mesh_only[shuffle_idx]
            sample_tex_real = sample_tex_real[shuffle_idx]
        if args.conditional_text:
            sample_text = sample_text[shuffle_idx]
        if args.conditional_semantics:
            sample_seg = sample_seg[shuffle_idx]
        #elif args.predict_semantics:
        #    sample_seg_fake = sample_seg_fake[shuffle_idx]
        #    raise NotImplemented

    if m_real_val is not None:
        # Make sure the number of images is the same as that of the test set
        if args.evaluate:
            np.random.seed(1234)
        val_indices = np.random.choice(len(emb_arr_fake_combined), size=n_images_val, replace=False)
        
        m1_val, s1_val = calculate_stats(emb_arr_fake_combined[val_indices])
        fid_val = calculate_frechet_distance(m1_val, s1_val, m_real_val, s_real_val)
        log('FID: {:.02f}'.format(fid_val))

        if has_pseudogt:
            m2_val, s2_val = calculate_stats(emb_arr_fake_texture_only[val_indices])
            fid_texture_val = calculate_frechet_distance(m2_val, s2_val, m_real_val, s_real_val)
            log('Texture-only FID: {:.02f}'.format(fid_texture_val))

            m3_val, s3_val = calculate_stats(emb_arr_fake_mesh_only[val_indices])
            fid_mesh_val = calculate_frechet_distance(m3_val, s3_val, m_real_val, s_real_val)
            log('Mesh-only FID: {:.02f}'.format(fid_mesh_val))
    
    if args.tensorboard and not args.evaluate:
        writer.add_image('image/real_tex', to_grid_tex(sample_tex_real), it)
        writer.add_image('image/fake_tex', to_grid_tex(sample_tex_fake), it)
        writer.add_image('image/fake_mesh', to_grid_mesh(sample_mesh_map_fake), it)
        
        grid_fake = torchvision.utils.make_grid(sample_fake, nrow=4)
        grid_fake_texture_only = torchvision.utils.make_grid(sample_fake_texture_only, nrow=4)
        grid_fake_mesh_only = torchvision.utils.make_grid(sample_fake_mesh_only, nrow=4)
        grid_real = torchvision.utils.make_grid(sample_real, nrow=4)
        writer.add_image('render/fake', grid_fake, it)
        writer.add_image('render/fake_texture', grid_fake_texture_only, it)
        writer.add_image('render/fake_mesh', grid_fake_mesh_only, it)
        writer.add_image('render/real', grid_real, it)
        
        if args.conditional_text:
            full_text = ''
            for idx, text in enumerate(sample_text):
                full_text += f'{idx}. '
                for wi in text:
                    wi = wi.item()
                    if wi == 0:
                        # Padding token
                        break
                    else:
                        full_text += train_ds.text_processor.ixtoword[wi] + ' '
                full_text += '  \n'
            writer.add_text('render/caption', full_text, it)
        
        if args.conditional_semantics:
            grid_seg = torchvision.utils.make_grid(sample_seg.data[:, :3], nrow=4)
            writer.add_image('image/real_seg', grid_seg, it)
        
        # elif args.predict_semantics:
            # raise NotImplemented
            
        if m_real_val is not None:
            writer.add_scalar('fid/combined', fid_val, it)
            if has_pseudogt:
                writer.add_scalar('fid/texture_only', fid_texture_val, it)
                writer.add_scalar('fid/mesh_only', fid_mesh_val, it)
        
    return fid_val



def divide_pred(pred):
    if pred is None:
        return None, None
    
    if type(pred) == list:
        fake = [x[:x.shape[0]//2] if x is not None else None for x in pred]
        real = [x[x.shape[0]//2:] if x is not None else None for x in pred]
    else:
        fake = pred[:pred.shape[0]//2]
        real = pred[pred.shape[0]//2:]

    return fake, real



def update_generator_running_avg(epoch):
    with torch.no_grad():
        # This heuristic does not affect the final result, it is just done for visualization purposes.
        # If alpha is very high (e.g. 0.999) it may take a while to visualize correct results on TensorBoard,
        # (or estimate reliable FID scores), therefore we lower alpha for the first few epochs.
        if epoch < 10:
            alpha = math.pow(args.g_running_average_alpha, 100)
        elif epoch < 100:
            alpha = math.pow(args.g_running_average_alpha, 10)
        else:
            alpha = args.g_running_average_alpha
        g_state_dict = generator.state_dict()
        for k, param in generator_running_avg.state_dict().items():
            if torch.is_floating_point(param):
                param.mul_(alpha).add_(g_state_dict[k], alpha=1-alpha)
            else:
                param.fill_(g_state_dict[k])

class ModelWrapper(nn.Module):
    
    def __init__(self, generator_instantiator, discriminator=None, text_encoder_instantiator=None):
        super().__init__()
        self.generator = generator_instantiator()
        self.generator_running_avg = generator_instantiator()
        self.generator_running_avg.load_state_dict(self.generator.state_dict()) # Same initial weights
        for p in self.generator_running_avg.parameters():
            p.requires_grad = False
        
        self.discriminator = discriminator
        self.criterion_gan = GANLoss(args.loss, tensor=torch.cuda.FloatTensor).cuda()
        
        if text_encoder_instantiator is not None:
            self.text_encoder_g = text_encoder_instantiator()
            total_params = 0
            for param in self.text_encoder_g.parameters():
                total_params += param.nelement()
            print('TextEncoder parameters: {:.2f}M'.format(total_params/1000000))
            if not args.evaluate:
                if not args.text_train_encoder:
                    # G and D use the same text encoder instance
                    self.text_encoder_d = self.text_encoder_g
                else:
                    # Different instances for G and D
                    self.text_encoder_d = text_encoder_instantiator()
        
    def forward(self, mode, X_tex, X_alpha, X_mesh=None, C=None, caption=None, seg=None, noise=None):
        assert mode in ['g', 'd', 'inference']
        if noise is None:
            noise = torch.randn((X_alpha.shape[0], args.latent_dim), device=X_alpha.device)
        
        if args.conditional_text:
            text_encoder = self.text_encoder_d if mode == 'd' else self.text_encoder_g
            words_emb, sent_emb = text_encoder(*caption)
            words_mask = (caption[0] == 0)
            caption = (words_emb, words_mask)
        
        if args.num_discriminators == 2 and args.texture_resolution >= 512:
            d_weight = [2, 1] # Texture discriminator has a larger weight on the loss
            
            if args.predict_semantics:
                d_weight += [1]
            
        else:
            d_weight = None # Unweighted
            
        if mode == 'g':
            pred_tex, pred_mesh, pred_seg = self.generator(noise, C, caption, seg)
            X_fake = torch.cat((pred_tex * X_alpha, X_alpha), dim=1) # Mask output
            X_fake_mesh = pred_mesh
            X_seg_fake = torch.cat((pred_seg * X_alpha, X_alpha), dim=1) # Mask output
            
            discriminated, mask = self.discriminator(X_fake, X_fake_mesh, C, caption, seg, X_seg_fake)
            loss = self.criterion_gan(discriminated, True, for_discriminator=False, mask=mask, weight=d_weight)
            
            return loss, pred_tex, pred_mesh, pred_seg
        
        elif mode == 'd':
            # D mode
            with torch.no_grad():
                pred_tex, pred_mesh, pred_seg = self.generator(noise, C, caption, seg)
                X_fake = torch.cat((pred_tex * X_alpha, X_alpha), dim=1) # Mask output
                X_fake_mesh = pred_mesh
                X_seg_fake = torch.cat((pred_seg * X_alpha, X_alpha), dim=1) # Mask output
                
                X_real = torch.cat((X_tex, X_alpha), dim=1)
                assert (X_mesh is None) == (pred_mesh is None)
                X_combined = torch.cat((X_fake, X_real), dim=0)
                C_combined = torch.cat((C, C), dim=0) if C is not None else None
                caption_combined = [torch.cat((x, x), dim=0) for x in caption] if caption is not None else None
                
                if args.predict_semantics:
                    assert seg is not None
                    X_seg_real = torch.cat((seg * X_alpha, X_alpha), dim=1)
                    seg_combined = torch.cat((X_seg_fake, X_seg_real), dim=0)
                    
                else:
                    seg_combined = torch.cat((seg, seg), dim=0) if seg is not None else None
                
                
                if pred_mesh is not None:
                    X_real_mesh = X_mesh
                    X_combined_mesh = torch.cat((X_fake_mesh, X_real_mesh), dim=0)
                    
                else:
                    X_combined_mesh = None
                    
                
                    
                    
            discriminated, mask = self.discriminator(X_combined, X_combined_mesh, C_combined, caption_combined, seg=seg, x_seg=seg_combined)
            discriminated_fake, discriminated_real = divide_pred(discriminated)
            mask_fake, mask_real = divide_pred(mask)
            loss_fake = self.criterion_gan(discriminated_fake, False, for_discriminator=True, mask=mask_fake, weight=d_weight)
            loss_real = self.criterion_gan(discriminated_real, True, for_discriminator=True, mask=mask_real, weight=d_weight)
            
            return loss_fake, loss_real, pred_tex, pred_mesh, pred_seg
        
        else:
            # Inference mode
            with torch.no_grad():
                pred_tex, pred_mesh, pred_seg, attn_map,  = self.generator_running_avg(noise, C, caption, seg, return_attention=True)
            return pred_tex, pred_mesh, pred_seg, attn_map



if args.norm_g == 'syncbatch':
    dataparallel = DataParallelWithCallback
    print('Using SyncBN')
else:
    dataparallel = nn.DataParallel

use_mesh = not args.texture_only

if args.conditional_text:
    text_encoder_instantiator = lambda: RNN_Encoder(train_ds.text_processor.n_words, args.text_max_length,
                                                    nhidden=args.text_embedding_dim)
else:
    text_encoder_instantiator = None
    
trainer = dataparallel(ModelWrapper(
    lambda: Generator(args, args.latent_dim, symmetric=args.symmetric_g, mesh_head=use_mesh),
    MultiScaleDiscriminator(args, 4) if not args.evaluate else None,
    text_encoder_instantiator,
).cuda(), gpu_ids)

generator = trainer.module.generator
generator_running_avg = trainer.module.generator_running_avg
discriminator = trainer.module.discriminator


if args.conditional_text:
    text_encoder_g = trainer.module.text_encoder_g
    if not args.evaluate:
        text_encoder_d = trainer.module.text_encoder_d
    if len(args.text_pretrained_encoder) > 0 and not args.evaluate:
        state_dict = torch.load(args.text_pretrained_encoder, map_location=lambda storage, loc: storage)
        text_encoder_g.load_state_dict(state_dict)
        if args.text_train_encoder and not args.evaluate:
            text_encoder_d.load_state_dict(state_dict)
        else:
            # No fine-tuning
            for p in text_encoder_g.parameters():
                p.requires_grad = False
            # No need to do it for text_encoder_d, the instance is the same
    elif not args.evaluate:
        assert args.text_train_encoder, 'The text encoder must be either pretrained or trainable'
    
    if args.evaluate:
        text_encoder_g.eval()
        if args.text_train_encoder:
            text_encoder_d.eval()

    if args.text_train_encoder:
        g_parameters = list(generator.parameters()) + list(text_encoder_g.parameters())
        d_parameters = list(discriminator.parameters()) + list(text_encoder_d.parameters())
    elif not args.evaluate:
        g_parameters = generator.parameters()
        d_parameters = discriminator.parameters()
        
elif not args.evaluate:
    g_parameters = generator.parameters()
    d_parameters = discriminator.parameters()
            
if not args.evaluate:
    optimizer_g = optim.Adam(g_parameters, lr=args.lr_g, betas=(0, 0.99))
    optimizer_d = optim.Adam(d_parameters, lr=args.lr_d, betas=(0, 0.99))

d_fake_curve = [0]
d_real_curve = [0]
g_curve = [0]
flat_curve = [0]
total_it = 0
epoch = 0

checkpoint_dir = 'checkpoints_gan/' + args.name
if args.continue_train or args.evaluate:
    # Load last checkpoint
    if args.which_epoch == 'best':
        which_epoch = 'latest' # Bypass (the search will be done later)
    else:
        which_epoch = args.which_epoch
    chk = torch.load(os.path.join(checkpoint_dir, f'checkpoint_{which_epoch}.pth'),
                     map_location=lambda storage, loc: storage)
    if 'epoch' in chk:
        epoch = chk['epoch']
        total_it = chk['iteration']
        g_curve = chk['g_curve']
        d_fake_curve = chk['d_fake_curve']
        d_real_curve = chk['d_real_curve']
        flat_curve = chk['flat_curve']
        generator.load_state_dict(chk['generator'])
    generator_running_avg.load_state_dict(chk['generator_running_avg'])
    if args.conditional_text:
        if not args.text_train_encoder or (args.evaluate and 'text_encoder_g' not in chk):
            text_encoder_g.load_state_dict(chk['text_encoder'])
        else:
            text_encoder_g.load_state_dict(chk['text_encoder_g'])
    if args.continue_train:
        optimizer_g.load_state_dict(chk['optimizer_g'])
        discriminator.load_state_dict(chk['discriminator'])
        optimizer_d.load_state_dict(chk['optimizer_d'])
        if args.conditional_text and args.text_train_encoder:
            text_encoder_d.load_state_dict(chk['text_encoder_d'])
        
        print(f'Resuming from epoch {epoch}')
    else:
        if 'epoch' in chk:
            print(f'Evaluating epoch {epoch}')
        args.epochs = -1 # Disable training
    chk = None

if args.tensorboard and not args.evaluate:
    log_dir = 'tensorboard_gan/' + args.name
    if not args.continue_train:
        shutil.rmtree(log_dir, ignore_errors=True) # Delete logs
    writer = SummaryWriter(log_dir)
else:
    writer = None
    
if not args.evaluate:
    pathlib.Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)
    log_file = open(os.path.join(checkpoint_dir, 'log.txt'), 'a' if args.continue_train else 'w', buffering=1) # Line buffering
    print(' '.join(sys.argv), file=log_file)
else:
    log_file = None

def log(text):
    if log_file is not None:
        print(text, file=log_file)
    print(text)


def to_grid_tex(x):
    with torch.no_grad():
        return torchvision.utils.make_grid((x.data[:, :3]+1)/2, nrow=4)

def to_grid_mesh(x):
    with torch.no_grad():
        x = x.data[:, :3]
        minv = x.min(dim=3, keepdim=True)[0].min(dim=2, keepdim=True)[0]
        maxv = x.max(dim=3, keepdim=True)[0].max(dim=2, keepdim=True)[0]
        x = (x - minv)/(maxv-minv)
        return torchvision.utils.make_grid(x, nrow=4)

try:
    while epoch < args.epochs:
        trainer.train()
        start_time = time.time()
        for i, data in enumerate(train_loader):
            X_tex = data['texture'].cuda()
            X_alpha = data['texture_alpha'].cuda()
            
            if args.conditional_class:
                C = data['class'].cuda()
                caption = None
            elif args.conditional_text:
                C = None
                caption = tuple([x.cuda() for x in data['caption']])
            else:
                C, caption = None, None
                
            if args.conditional_semantics:
                seg = data['seg'].cuda()
            
            elif args.predict_semantics:
                seg = data['seg_inv_rend'].cuda()
            
            else:
                seg = None

            
            if use_mesh:
                X_mesh = data['mesh'].cuda()
            else:
                X_mesh = None

            if total_it % (1 + args.d_steps_per_g) == 0:
                # --------------------------------------------- Generator loop
                optimizer_g.zero_grad()
                
                loss, pred_tex, pred_mesh, pred_seg = trainer('g', None, X_alpha, None, C, caption, seg)
                
                if use_mesh:
                    vtx = mesh_template.get_vertex_positions(pred_mesh)
                    flat_loss = loss_flat(mesh_template.mesh, mesh_template.compute_normals(vtx))
                    flat_curve.append(flat_loss.item())
                else:
                    flat_loss = 0

                loss_gan = loss.mean()
                loss = loss_gan + args.mesh_regularization*flat_loss
                loss.backward()
                optimizer_g.step()
                update_generator_running_avg(epoch)
                g_curve.append(loss_gan.item())
                if args.tensorboard:
                    writer.add_scalar(f'gan_{args.loss}/g', loss_gan.item(), total_it)
                    if use_mesh:
                        writer.add_scalar('flat', flat_loss.item(), total_it)
                        
            else:
                # --------------------------------- Discriminator loop
                optimizer_d.zero_grad()

                loss_fake, loss_real, pred_tex, pred_mesh, pred_seg = trainer('d', X_tex, X_alpha, X_mesh, C, caption, seg)
                loss_fake = loss_fake.mean()
                loss_real = loss_real.mean()
                loss = loss_fake + loss_real
                loss.backward()
                optimizer_d.step()
                d_fake_curve.append(loss_fake.item())
                d_real_curve.append(loss_real.item())
                if args.tensorboard:
                    writer.add_scalar(f'gan_{args.loss}/d_fake_loss', loss_fake.item(), total_it)
                    writer.add_scalar(f'gan_{args.loss}/d_real_loss', loss_real.item(), total_it)

            if total_it % 10 == 0:
                log('[{}] epoch {}, {}/{}, g_loss {:.5f} d_fake_loss {:.5f} d_real_loss {:.5f} flat {:.5f}'.format(
                                                                        total_it, epoch, i, len(train_loader),
                                                                        g_curve[-1], d_fake_curve[-1], d_real_curve[-1],
                                                                        flat_curve[-1]))
            
            total_it += 1
        
        epoch += 1
        
        log('Time per epoch: {:.3f} s'.format(time.time() - start_time))

        if epoch >= args.lr_decay_after and epoch < args.epochs:
            factor = 1 - min(max((epoch - args.lr_decay_after)/(args.epochs - args.lr_decay_after), 0), 1)
            for param_group in optimizer_g.param_groups:
                param_group['lr'] = args.lr_g * factor
            for param_group in optimizer_d.param_groups:
                param_group['lr'] = args.lr_d * factor
                
        def save_checkpoint(it):
            out_dict = {
                'optimizer_g': optimizer_g.state_dict(),
                'optimizer_d': optimizer_d.state_dict(),
                'generator': generator.state_dict(),
                'generator_running_avg': generator_running_avg.state_dict(),
                'discriminator': discriminator.state_dict(),
                'epoch': epoch,
                'iteration': total_it,
                'g_curve': g_curve,
                'd_fake_curve': d_fake_curve,
                'd_real_curve': d_real_curve,
                'flat_curve': flat_curve,
                'args': vars(args),
            }
            if args.conditional_text:
                if not args.text_train_encoder:
                    out_dict['text_encoder'] = text_encoder_g.state_dict()
                else:
                    out_dict['text_encoder_g'] = text_encoder_g.state_dict()
                    out_dict['text_encoder_d'] = text_encoder_d.state_dict()
            torch.save(out_dict, os.path.join(checkpoint_dir, f'checkpoint_{it}.pth'))
    
        if epoch % args.save_freq == 0:
            save_checkpoint('latest')
        if epoch % args.checkpoint_freq == 0:
            save_checkpoint(str(epoch))
        if epoch % args.evaluate_freq == 0 and not args.texture_only:
            evaluate_fid(writer, total_it, data['idx'])
        
except KeyboardInterrupt:
    print('Aborted.')
    
if not args.evaluate:
    save_checkpoint('latest')
elif args.evaluate and not args.export_sample:
    # FID evaluation mode
    
    if args.which_epoch == 'best':
        import re
        
        best_fid = float('inf')
        best_checkpoint = None
        
        checkpoints = {}
        # Run search on all checkpoints and select best FID
        chk_paths = sorted(glob.glob(os.path.join(checkpoint_dir, f'checkpoint_[0-9]*.pth')))
        for path in chk_paths:
            chk_epoch = re.search('checkpoint_([0-9]+).pth', path)
            if chk_epoch:
                checkpoints[int(chk_epoch.group(1))] = path
            else:
                raise ValueError(f'Invalid path detected: {path}')
        
        print('Enumerating checkpoints:')
        checkpoints = sorted(checkpoints.items())[::-1]
        for chk_epoch, path in checkpoints:
            print(f'Epoch {chk_epoch}: {path}')
        
        if len(checkpoints) == 0:
            print('No saved checkpoints found')
            sys.exit(1)
        
        def load_checkpoint(path):
            chk = torch.load(path, map_location=lambda storage, loc: storage)
            generator_running_avg.load_state_dict(chk['generator_running_avg'])
            generator.load_state_dict(chk['generator'])
        
        
        for chk_epoch, path in checkpoints:
            print(f'--- Evaluating epoch {chk_epoch} ---')
            load_checkpoint(path)
            try:
                fid = evaluate_fid(writer, total_it, fast=True)
            except KeyboardInterrupt:
                print('Aborted.')
                break
                
            if fid < best_fid:
                best_fid = fid
                best_checkpoint = path
            print(f'Best FID so far: {best_fid} at {best_checkpoint}')
            
        print(f'--- Running final evaluation using {best_checkpoint} ---')
        load_checkpoint(best_checkpoint)
        
            

    # Evaluate specified checkpoint
    evaluate_fid(writer, total_it)
    
elif args.export_sample:
    if args.how_many == -1:
        args.how_many = args.batch_size
    print(f'Exporting sample of {args.how_many} objects')
    with torch.no_grad():
        num_passes = args.how_many // args.batch_size
        exported_images = []
        for i_pass in tqdm(range(num_passes)):
            indices = np.random.choice(len(train_ds), size=args.batch_size, replace=False)
            if args.conditional_class or args.conditional_text:
                if args.conditional_class:
                    c = torch.LongTensor([train_ds.classes[i] for i in indices]).cuda()
                    if len(c.shape) == 1:
                        c = c.unsqueeze(-1)
                    caption = None
                elif args.conditional_text:
                    c = None
                    caps = []
                    cap_lengths = []
                    for i in indices:
                        cap, cap_length = train_ds.get_random_caption(i)
                        caps.append(cap)
                        cap_lengths.append(cap_length)
                    caption = (torch.LongTensor(caps).cuda(), torch.LongTensor(cap_lengths).cuda())
            else:
                c, caption = None, None

            if args.conditional_semantics:
                seg = []
                for i in indices:
                    seg.append(train_ds[i]['seg'])
                seg = torch.stack(seg, dim=0)
            else:
                seg = None


            noise = torch.randn(args.batch_size, args.latent_dim)

            # Gaussian truncation trick
            sigma = args.truncation_sigma
            while (noise.abs() > sigma).any():
                # Rejection sampling
                mask = noise.abs() > sigma
                noise[mask] = torch.randn_like(noise[mask])

            generator_running_avg.eval()
            noise = noise.cuda()
            pred_tex, pred_mesh_map, pred_seg, attn_map = trainer('inference', None, None, C=c, caption=caption, seg=seg, noise=noise)
            
            vtx = mesh_template.get_vertex_positions(pred_mesh_map)
            vtx_obj = vtx.clone()
            vtx_obj[..., :] = vtx_obj[..., [0, 2, 1]] # Swap Y and Z (the result is Y up)
            output_dir = os.path.join('output', args.name)
            pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)
            for i, v in enumerate(vtx_obj):
                j = i + i_pass * args.batch_size
                mesh_name = f'mesh_{j:0>6d}'
                mesh_template.export_obj(os.path.join(output_dir, f'{mesh_name}'), v, pred_tex[i]/2 + 0.5)
            
                # Export segmentation maps
                if args.predict_semantics:
                    seg_obj = np.array(pred_seg[i].argmax(dim=0).cpu())
                    np.save(os.path.join(output_dir, f'{mesh_name}_seg.npy'), seg_obj)
            
            rotation = train_ds.data['rotation'][indices].cuda()
            scale = train_ds.data['scale'][indices].cuda()
            translation = train_ds.data['translation'][indices].cuda()

            vtx = qrot(rotation, scale.unsqueeze(-1)*vtx) + translation.unsqueeze(1)
            vtx = vtx * torch.Tensor([1, -1, -1]).to(vtx.device)

            image_pred, alpha_pred = mesh_template.forward_renderer(renderer, vtx, pred_tex,
                                                                    num_gpus=len(gpu_ids),
                                                                    return_hardmask=True)
            image_pred[alpha_pred.expand_as(image_pred) == 0] = 1
            image_pred = image_pred.permute(0, 3, 1, 2)/2 + 0.5
            image_pred = torch.cat((image_pred, alpha_pred.permute(0, 3, 1, 2)), dim=1) # Add alpha channel
            image_pred = F.avg_pool2d(image_pred, 4) # Anti-aliasing
            exported_images.append(image_pred.cpu())

        print('Saving...')
        exported_images = torch.cat(exported_images, dim=0)
        import imageio
        import torchvision
        image_grid = torchvision.utils.make_grid(exported_images, nrow=8, padding=0)
        image_grid = (image_grid.permute(1, 2, 0)*255).clamp(0, 255).cpu().byte().numpy()
        imageio.imwrite(f'output/{args.name}.png', image_grid)
    
    
if writer is not None:
    writer.close()
    
print('Done.')
