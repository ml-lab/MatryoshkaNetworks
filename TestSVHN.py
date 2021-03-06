import os
import json
from time import time
import numpy as np
from tqdm import tqdm
from matplotlib import pyplot as plt
from sklearn.externals import joblib

import theano
import theano.tensor as T
from theano.sandbox.cuda.dnn import dnn_conv
from theano.sandbox.cuda.rng_curand import CURAND_RandomStreams as RandStream

#
# DCGAN paper repo stuff
#
from lib import activations
from lib import updates
from lib import inits
from lib.vis import color_grid_vis
from lib.rng import py_rng, np_rng
from lib.ops import batchnorm, conv_cond_concat, deconv, dropout
from lib.theano_utils import floatX, sharedX
from lib.data_utils import shuffle, iter_data
from load import load_svhn

#
# Phil's business
#
from MatryoshkaModules import DiscConvModule, DiscFCModule, GenConvModule, \
                              GenFCModule, BasicConvModule, GenUniModule

# path for dumping experiment info and fetching dataset
EXP_DIR = "./svhn"

# setup paths for dumping diagnostic info
desc = 'matronet_2'
model_dir = "{}/models/{}".format(EXP_DIR, desc)
sample_dir = "{}/samples/{}".format(EXP_DIR, desc)
log_dir = "{}/logs".format(EXP_DIR)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
if not os.path.exists(model_dir):
    os.makedirs(model_dir)
if not os.path.exists(sample_dir):
    os.makedirs(sample_dir)

# locations of 32x32 SVHN dataset
tr_file = "{}/data/svhn_train.pkl".format(EXP_DIR)
te_file = "{}/data/svhn_test.pkl".format(EXP_DIR)
ex_file = "{}/data/svhn_extra.pkl".format(EXP_DIR)
# load dataset (load more when using adequate computers...)
data_dict = load_svhn(tr_file, te_file, ex_file=ex_file, ex_count=150000)

# stack data into a single array and rescale it into [-1,1]
Xtr = np.concatenate([data_dict['Xtr'], data_dict['Xte'], data_dict['Xex']], axis=0)
del data_dict
Xtr = Xtr - np.min(Xtr)
Xtr = Xtr / np.max(Xtr)
Xtr = 2.0 * (Xtr - 0.5)


k = 1             # # of discrim updates for each gen update
l2 = 2.0e-5       # l2 weight decay
b1 = 0.5          # momentum term of adam
nc = 3            # # of channels in image
nbatch = 128      # # of examples in batch
npx = 32          # # of pixels width/height of images
nz0 = 64          # # of dim for Z0
nz1 = 8           # # of dim for Z1
ngfc = 256        # # of gen units for fully connected layers
ndfc = 256        # # of discrim units for fully connected layers
ngf = 32          # # of gen filters in first conv layer
ndf = 32          # # of discrim filters in first conv layer
nx = npx*npx*nc   # # of dimensions in X
niter = 150       # # of iter at starting learning rate
niter_decay = 200 # # of iter to linearly decay learning rate to zero
lr = 0.0001       # initial learning rate for adam
ntrain = Xtr.shape[0]

def train_transform(X):
    # transform vectorized observations into convnet inputs
    return X.reshape(-1, nc, npx, npx).transpose(0, 1, 2, 3)

def draw_transform(X):
    # transform vectorized observations into drawable images
    X = (X + 1.0) * 127.0
    return X.reshape(-1, nc, npx, npx).transpose(0, 2, 3, 1)

def rand_gen(size):
    #r_vals = floatX(np_rng.uniform(-1., 1., size=size))
    r_vals = floatX(np_rng.normal(size=size))
    return r_vals

# draw some examples from training set
color_grid_vis(draw_transform(Xtr[0:200]), (10, 20), "{}/Xtr.png".format(sample_dir))


tanh = activations.Tanh()
sigmoid = activations.Sigmoid()
bce = T.nnet.binary_crossentropy

gifn = inits.Normal(scale=0.02)
difn = inits.Normal(scale=0.02)

#
# Define some modules to use in the generator
#
gen_module_1 = \
GenUniModule(
    rand_dim=nz0,
    out_dim=(ngf*8*2*2),
    apply_bn=True,
    init_func=gifn,
    rand_type='normal',
    mod_name='gen_mod_1'
) # output is (batch, ngf*8*2*2)

gen_module_2 = \
GenConvModule(
    filt_shape=(3,3),
    in_chans=(ngf*8),
    out_chans=(ngf*8),
    rand_chans=nz1,
    apply_bn_1=True,
    apply_bn_2=True,
    us_stride=2,
    init_func=gifn,
    use_rand=False,
    use_pooling=False,
    rand_type='normal',
    mod_name='gen_mod_2'
) # output is (batch, ngf*4, 4, 4)

gen_module_3 = \
GenConvModule(
    filt_shape=(3,3),
    in_chans=(ngf*8),
    out_chans=(ngf*4),
    rand_chans=nz1,
    apply_bn_1=True,
    apply_bn_2=True,
    us_stride=2,
    init_func=gifn,
    use_rand=True,
    use_pooling=False,
    rand_type='normal',
    mod_name='gen_mod_3'
) # output is (batch, ngf*4, 8, 8)

gen_module_4 = \
GenConvModule(
    filt_shape=(3,3),
    in_chans=(ngf*4),
    out_chans=(ngf*2),
    rand_chans=nz1,
    apply_bn_1=True,
    apply_bn_2=True,
    us_stride=2,
    init_func=gifn,
    use_rand=True,
    use_pooling=False,
    rand_type='normal',
    mod_name='gen_mod_4'
)  # output is (batch, ngf*2, 16, 16)

gen_module_5 = \
GenConvModule(
    filt_shape=(5,5),
    in_chans=(ngf*2),
    out_chans=(ngf*2),
    rand_chans=nz1,
    apply_bn_1=True,
    apply_bn_2=True,
    us_stride=2,
    init_func=gifn,
    use_rand=True,
    use_pooling=False,
    rand_type='normal',
    mod_name='gen_mod_5'
)  # output is (batch, ngf*1, 32, 32)

# weights for final convolutional "aggregation layer"
gwx = gifn((nc, (ngf*2), 5, 5), 'gwx')

#
# Define some modules to use in the discriminator
#
disc_module_1 = \
DiscConvModule(
    filt_shape=(5,5),
    in_chans=nc,
    out_chans=ndf,
    apply_bn_1=False,
    apply_bn_2=True,
    ds_stride=2,
    use_pooling=False,
    init_func=difn,
    mod_name='disc_mod_1'
) # output is (batch, ndf, 16, 16)

disc_module_2 = \
DiscConvModule(
    filt_shape=(3,3),
    in_chans=(ndf*1),
    out_chans=(ndf*2),
    apply_bn_1=True,
    apply_bn_2=True,
    ds_stride=2,
    use_pooling=False,
    init_func=difn,
    mod_name='disc_mod_2'
) # output is (batch, ndf*2, 8, 8)

disc_module_3 = \
DiscConvModule(
    filt_shape=(3,3),
    in_chans=(ndf*2),
    out_chans=(ndf*4),
    apply_bn_1=True,
    apply_bn_2=True,
    ds_stride=2,
    use_pooling=False,
    init_func=difn,
    mod_name='disc_mod_3'
) # output is (batch, ndf*4, 4, 4)

disc_module_4 = \
DiscConvModule(
    filt_shape=(3,3),
    in_chans=(ndf*4),
    out_chans=(ndf*8),
    apply_bn_1=True,
    apply_bn_2=True,
    ds_stride=2,
    use_pooling=False,
    init_func=difn,
    mod_name='disc_mod_4'
) # output is (batch, ndf*8, 2, 2)

disc_module_5 = \
DiscFCModule(
    fc_dim=ndfc,
    in_dim=(ndf*8*2*2),
    apply_bn=True,
    init_func=difn,
    mod_name='disc_mod_5'
) # output is (batch, 1)
 
#
# Grab parameters from generator and discriminator
#
gen_params = gen_module_1.params + \
             gen_module_2.params + \
             gen_module_3.params + \
             gen_module_4.params + \
             gen_module_5.params + \
             [gwx]

discrim_params = disc_module_1.params + \
                 disc_module_2.params + \
                 disc_module_3.params + \
                 disc_module_4.params + \
                 disc_module_5.params

def gen(Z0, wx):
    # feedforward through the fully connected part of generator
    h2 = gen_module_1.apply(rand_vals=Z0)
    # reshape as input to a conv layer (in 2x2 grid)
    h2 = h2.reshape((h2.shape[0], ngf*8, 2, 2))
    # feedforward through convolutional generator module
    h3 = gen_module_2.apply(h2, rand_vals=None)
    # feedforward through convolutional generator module
    h4 = gen_module_3.apply(h3, rand_vals=None)
    # feedforward through convolutional generator module
    h5 = gen_module_4.apply(h4, rand_vals=None)
    # feedforward through convolutional generator module
    h6 = gen_module_5.apply(h5, rand_vals=None)
    # feedforward through another conv and clamp to [0,1]
    h7 = dnn_conv(h6, wx, subsample=(1, 1), border_mode=(2, 2))
    x = tanh(h7)
    return x

def discrim(X):
    # apply 3x3 double conv discriminator module
    h1, y1 = disc_module_1.apply(X)
    # apply 3x3 double conv discriminator module
    h2, y2 = disc_module_2.apply(h1)
    # apply 3x3 double conv discriminator module
    h3, y3 = disc_module_3.apply(h2)
    # apply 3x3 double conv discriminator module
    h4, y4 = disc_module_4.apply(h3)
    # concat label info and feedforward through fc module
    h4 = T.flatten(h4, 2)
    y5 = disc_module_5.apply(h4)
    return y1, y2, y3, y4, y5

X = T.tensor4()
Z0 = T.matrix()

# draw samples from the generator
gX = gen(Z0, gwx)

# feed real data and generated data through discriminator
p_real = discrim(X)
p_gen = discrim(gX)

# compute costs based on discriminator output for real/generated data
d_cost_real = sum([bce(p, T.ones(p.shape)).mean() for p in p_real])
d_cost_gen = sum([bce(p, T.zeros(p.shape)).mean() for p in p_gen])
g_cost_d = sum([bce(p, T.ones(p.shape)).mean() for p in p_gen])

# d_cost_real = bce(p_real[-1], T.ones(p_real[-1].shape)).mean()
# d_cost_gen = bce(p_gen[-1], T.zeros(p_gen[-1].shape)).mean()
# g_cost_d = bce(p_gen[-1], T.ones(p_gen[-1].shape)).mean()

d_cost = d_cost_real + d_cost_gen + (1e-5 * sum([T.sum(p**2.0) for p in discrim_params]))
g_cost = g_cost_d + (1e-5 * sum([T.sum(p**2.0) for p in gen_params]))

cost = [g_cost, d_cost, g_cost_d, d_cost_real, d_cost_gen]

lrt = sharedX(lr)
d_updater = updates.Adam(lr=lrt, b1=b1, regularizer=updates.Regularizer(l2=l2))
g_updater = updates.Adam(lr=lrt, b1=b1, regularizer=updates.Regularizer(l2=l2))
d_updates = d_updater(discrim_params, d_cost)
g_updates = g_updater(gen_params, g_cost)
updates = d_updates + g_updates

print 'COMPILING'
t = time()
_train_g = theano.function([X, Z0], cost, updates=g_updates)
_train_d = theano.function([X, Z0], cost, updates=d_updates)
_gen = theano.function([Z0], gX)
print "{0:.2f} seconds to compile theano functions".format(time()-t)


f_log = open("{}/{}.ndjson".format(log_dir, desc), 'wb')
log_fields = [
    'n_epochs', 
    'n_updates', 
    'n_examples', 
    'n_seconds',
    'g_cost',
    'd_cost',
]

print desc.upper()
n_updates = 0
n_check = 0
n_epochs = 0
n_updates = 0
n_examples = 0
t = time()
sample_z0mb = rand_gen(size=(200, nz0)) # noise samples for top generator module
for epoch in range(1, niter+niter_decay+1):
    Xtr = shuffle(Xtr)
    g_cost = 0
    d_cost = 0
    gc_iter = 0
    dc_iter = 0
    for imb in tqdm(iter_data(Xtr, size=nbatch), total=ntrain/nbatch):
        imb = train_transform(imb)
        z0mb = rand_gen(size=(len(imb), nz0))
        if n_updates % (k+1) == 0:
            g_cost += _train_g(imb, z0mb)[0]
            gc_iter += 1
        else:
            d_cost += _train_d(imb, z0mb)[1]
            dc_iter += 1
        n_updates += 1
        n_examples += len(imb)
    print("g_cost: {0:.4f}, d_cost: {1:.4f}".format((g_cost/gc_iter),(d_cost/dc_iter)))
    samples = np.asarray(_gen(sample_z0mb))
    color_grid_vis(draw_transform(samples), (10, 20), "{}/{}.png".format(sample_dir, n_epochs))
    n_epochs += 1
    if n_epochs > niter:
        lrt.set_value(floatX(lrt.get_value() - lr/niter_decay))
    if n_epochs in [1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 150, 200, 250, 300]:
        joblib.dump([p.get_value() for p in gen_params], "{}/{}_gen_params.jl".format(model_dir, n_epochs))
        joblib.dump([p.get_value() for p in discrim_params], "{}/{}_discrim_params.jl".format(model_dir, n_epochs))
