import numpy as np
import sys
sys.path.append('../..')
import time

from flare import struc, gp, env, mc_simple
from flare.mff.mff_mc import MappedForceField
import matplotlib.pyplot as plt

# ------------- load lj
t0 = time.time()
lj_species = np.load('lj_species.npy')
lj_pos = np.load('lj_pos.npy')
lj_forces = np.load('lj_forces.npy')

# set cell
cell = np.eye(3) * 8.092

# ------------- create training structures
# train_snaps = [100, 200, 300, 400]
train_snaps = [100]
train_strucs = []
train_forces = []
for snap in train_snaps:
    train_strucs.append(struc.Structure(cell, lj_species[snap], lj_pos[snap]))
    train_forces.append(lj_forces[snap])

# ------------- create test structures
test_snaps = [900] #, 600, 700, 800, 900]
test_strucs = []
test_forces = []
for snap in test_snaps:
    test_strucs.append(struc.Structure(cell, lj_species[snap], lj_pos[snap]))
    test_forces.append(lj_forces[snap])
print('data ready:', time.time() - t0)

# ------------- initialize gp model
t0 = time.time()
kernel = mc_simple.two_plus_three_body_mc
kernel_grad = mc_simple.two_plus_three_body_mc_grad
energy_force_kernel = mc_simple.two_plus_three_mc_force_en
hyps = np.array([0.5, 1, 0.001, 1, 0.1])
#kernel = mc_simple.three_body_mc
#kernel_grad = mc_simple.three_body_mc_grad
#energy_force_kernel = mc_simple.three_body_mc_force_en
#hyps = np.array([1, 1, 1])
cutoffs = np.array([3.5, 3.5])
opt_algorithm = 'BFGS'
maxiter = 1000

gp_model = gp.GaussianProcess(kernel, kernel_grad, hyps, cutoffs,
                              energy_force_kernel=energy_force_kernel,
                              opt_algorithm=opt_algorithm,
                              maxiter=maxiter)

# --------------- train gp model
for struc_curr, forces in zip(train_strucs, train_forces):
    gp_model.update_db(struc_curr, forces)

gp_model.set_L_alpha()
#gp_model.train(False) #True)
print('gp built:', time.time() - t0)

# --------------- set mff params
t0 = time.time()
unit = 0.00010364269933008285
struc_params = {'species': [0, 1], 
                'cube_lat': cell,
                'mass_dict': {'0': 27 * unit, '1': 16 * unit}}
 
grid_params = {'bounds_2': [[1.2], [3.5]],
               'bounds_3': [[1.2, 1.2, 0], [3.5, 3.5, np.pi]],
               'grid_num_2': 8,
               'grid_num_3': [8, 8, 8],
               'svd_rank': 8,
               'bodies': [2, 3],
               'load_grid': None,
               'load_svd': None} 

mff_model = MappedForceField(gp_model, grid_params, struc_params)
print('mff model built:', time.time() - t0)

# --------------- test gp model and mff model
store_predictions = []
store_mffpred = []
store_forces = []
var_err = 0
for n, snap in enumerate(test_snaps):
    structure = test_strucs[n]
    forces = test_forces[n]
    noa = len(structure.positions)

    predictions = np.zeros([noa, 3])
    variances = np.zeros([noa, 3])
    mff_pred = np.zeros([noa, 3])
    mff_var = np.zeros([noa, 3])

#    for atom in range(noa):
#        env_curr = env.AtomicEnvironment(structure, atom, cutoffs)
#
#        for m in range(3):
#            d = m+1
#            comp_pred, comp_var = gp_model.predict(env_curr, d)
#            predictions[atom, m] = comp_pred
#            variances[atom, m] = comp_var
#
#        mff_f, mff_v = mff_model.predict(env_curr, mean_only=False)
#        mff_pred[atom, :] = mff_f
#        mff_var[atom, :] = mff_v
#        var_err += np.mean(np.absolute(mff_v-variances[atom, :]))
##        print(env_curr.ctype, mff_v, variances[atom])

    store_predictions.append(predictions)
    store_mffpred.append(mff_pred)
    store_forces.append(forces)

    # test a toy case for testing lammps
    positions = np.array([[0,0,0], [0,0,1.76339], [0,1.41071,0]])
    species_list = ['H', 'C', 'H']
    new_cell = np.eye(3) * 20
    toy_struc = struc.Structure(new_cell, species_list, positions)
    for atom in range(3):
        atom_env = env.AtomicEnvironment(toy_struc, atom, cutoffs)
        pred_f, pred_v = mff_model.predict(atom_env, mean_only=True)
        print(pred_f)

# ----------------- save coeffs ---------------------
if 2 in grid_params['bodies']:
    for ind, spc in enumerate(mff_model.spcs[0]):
        save_name = '{}{}_{}'.format(spc[0],spc[1],2)
        np.save(save_name, mff_model.maps_2[ind].mean.model.__coeffs__)

if 3 in grid_params['bodies']:
    for ind, spc in enumerate(mff_model.spcs[1]):
        save_name = '{}{}{}_{}'.format(spc[0],spc[1],spc[2],3)
        np.save(save_name, mff_model.maps_3[ind].mean.model.__coeffs__)

# ----------------- save predictions and ground truth
store_predictions = np.array(store_predictions).reshape(-1)
store_mffpred = np.array(store_mffpred).reshape(-1)
store_forces = np.array(store_forces).reshape(-1)
np.save('store_predictions', store_predictions)
np.save('store_forces', store_forces)

# ----------------- get prediction err
gp_err = np.mean(np.absolute(store_predictions-store_forces))
mff_err = np.mean(np.absolute(store_mffpred-store_forces))
mff_gp = np.mean(np.absolute(store_mffpred-store_predictions))
var_err /= noa * len(test_snaps)
print('gp err:', gp_err)
print('mff err:', mff_err)
print('mff - gp:', mff_gp)
print('var err:', var_err)

## ----------------- reconstruct lj profiles
#cell = np.eye(3) * 10
#species_list = np.array([[0, 0], [0, 1], [1, 1]], dtype=np.int8)
#seps = np.linspace(0.5, 4, 1000)
#cutoffs = np.array([3.5])
#preds = np.zeros((3, seps.shape[0]))
#variances = np.zeros((3, seps.shape[0]))
#
#for n, sep in enumerate(seps):
#    positions = np.array([[0, 0, 0], [sep, 0, 0]])
#
#    for m, species in enumerate(species_list):
#        struc_curr = struc.Structure(cell, species, positions)
#        struc_curr.coded_species = species
#        struc_curr.nos = 2
#        env_curr = env.AtomicEnvironment(struc_curr, 0, cutoffs)
#        pred_curr = gp_model.predict(env_curr, 1)
#        preds[m, n] = pred_curr[0]
#        variances[m, n] = pred_curr[1]
#
## save reconstructed lj profiles
#np.save('lj_seps', seps)
#np.save('lj_preds', preds)
#np.save('lj_vars', variances)
