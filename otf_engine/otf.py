import sys
import numpy as np
import datetime
import time
from typing import List
from struc import Structure
from gp import GaussianProcess
from env import ChemicalEnvironment
from qe_util import run_espresso, parse_qe_input, \
    qe_input_to_structure, parse_qe_forces
import multiprocessing as mp
import concurrent.futures


class OTF(object):
    def __init__(self, qe_input: str, dt: float, number_of_steps: int,
                 gp: GaussianProcess, cutoff: float, pw_loc: str,
                 std_tolerance_factor: float = 1, opt_algo: str='BFGS',
                 prev_pos_init: np.ndarray=None, par: bool=False,
                 parsimony: bool=False, skip: int=0, hyps: np.ndarray=None,
                 init_atoms: List[int]=None):
        """Generates an on-the-fly molecular dynamics trajectory.

        :param qe_input: location of quantum espresso input file
        :type qe_input: str
        :param dt: time step in ps
        :type dt: float
        :param number_of_steps: number of MD steps in otf run
        :type number_of_steps: int
        :param cutoff: cutoff radius. atoms outside this region are not
        considered in force calculations.
        :type cutoff: float
        :param pw_loc: location of quantum espresso pw.x executable.
        :type pw_loc: str
        :param std_tolerance_factor: negative tolerance is a hard user-defined
            std cutoff. positive is a multiple of the noise std. 0 means no
            failure condition (DFT is off). defaults to 1.
        :param std_tolerance_factor: float, optional
        :param opt_algo: optimization algorithm for setting hyperparameters.
            defaults to 'BFGS'.
        :param opt_algo: str, optional
        :param prev_pos_init: array of previous positions. determines the
            initial velocities of the atoms. defaults to None.
        :param prev_pos_init: np.ndarray, optional
        :param par: if True, force calculations are parallelized using
            Python's concurrent.futures class. defaults to False.
        :param par: bool, optional
        :param parsimony: if True, only the highest uncertainty atom is added
            to the training database after the first DFT call. defaults to
            False.
        :param parsimony: bool, optional
        :param cutoffs: array of cutoffs. only used for combination
            kernels, e.g. 2+3-body. defaults to None.
        :param cutoffs: np.ndarray, optional
        :param skip: number of frames skipped before printing to the
            output file. defaults to 0.
        :param skip: int, optional
        :param hyps: array of hyperparameters. if None, the hyperparameters
            are re-optimized after every DFT call. if not None, then the
            hyperparameters are fixed throughout the run. defaults to None.
        :param hyps: np.ndarray, optional
        :param init_atoms: list of atoms to put in the training set after the
            first DFT call. if None, all atoms are placed in the training set.
            defaults to None.
        :param init_atoms: List[int], optional.
        """

        self.qe_input = qe_input
        self.dt = dt
        self.Nsteps = number_of_steps
        self.gp = gp
        self.cutoff = cutoff
        self.std_tolerance = std_tolerance_factor
        self.pw_loc = pw_loc

        # allow hyperparameters to be set
        if hyps is not None:
            self.gp.hyps = hyps
            self.freeze_hyps = True
        else:
            self.freeze_hyps = False

        # parse input file
        positions, species, cell, masses = parse_qe_input(self.qe_input)

        self.structure = Structure(cell=cell, species=species,
                                   positions=positions, cutoff=cutoff,
                                   mass_dict=masses,
                                   prev_positions=prev_pos_init)

        self.velocities = (self.structure.positions -
                           self.structure.prev_positions) / self.dt

        self.noa = self.structure.positions.shape[0]
        self.local_energies = np.zeros(self.noa)
        self.atom_list = list(range(self.noa))
        self.curr_step = 0
        self.write_header()

        if init_atoms is None:
            self.init_atoms = [int(n) for n in range(self.noa)]
        else:
            self.init_atoms = init_atoms

        self.par = par
        self.parsimony = parsimony
        self.skip = skip
        self.dft_count = 0

    def run(self):
        counter = 0
        self.start_time = time.time()

        while self.curr_step < self.Nsteps:
            # run DFT and train initial model if first step and DFT is on
            if self.curr_step == 0 and self.std_tolerance != 0:
                self.run_and_train()
                self.write_md_config()
                self.update_positions()

            # otherwise, try predicting with GP model
            else:
                if self.par is False:
                    self.predict_on_structure()
                else:
                    self.predict_on_structure_par()

                std_in_bound, target_atom = self.is_std_in_bound()
                if not std_in_bound:
                    self.dft_count += 1
                    self.write_md_config()
                    self.run_and_train(target_atom)
                    self.write_md_config()

                # write gp forces only when counter equals skip
                if counter >= self.skip and self.structure.dft_forces is False:
                    self.write_md_config()
                    counter = -1
                counter += 1

                self.update_positions()

        self.conclude_run()

    def run_and_profile(self):
        self.start_time = time.time()

        while self.curr_step < self.Nsteps:
            # run DFT and train initial model if first step and DFT is on
            if self.curr_step == 0 and self.std_tolerance != 0:
                self.run_and_train()
                self.write_md_config()
                self.update_positions()

            # otherwise, try predicting with GP model
            else:
                time0 = time.time()
                if self.par is False:
                    self.predict_on_structure()
                else:
                    self.predict_on_structure_par()
                time1 = time.time()
                pred_time = time1 - time0

                std_in_bound, target_atom = self.is_std_in_bound()
                if not std_in_bound:
                    self.write_md_config()
                    self.run_and_train(target_atom)

                time2 = time.time()
                std_time = time2 - time1

                self.write_md_config()
                time3 = time.time()
                write_time = time3 - time2

                self.update_positions()
                time4 = time.time()
                update_time = time4 - time3

                self.write_time_info(pred_time, std_time, write_time,
                                     update_time)

        self.conclude_run()

    def predict_on_atom(self, atom):
        chemenv = ChemicalEnvironment(self.structure, atom)
        comps = []
        stds = []
        # predict force components and standard deviations
        for i in range(3):
            force, var = self.gp.predict(chemenv, i+1)
            comps.append(float(force))
            stds.append(np.sqrt(np.absolute(var)))

        # predict local energy
        local_energy = self.gp.predict_local_energy(chemenv)
        return comps, stds, local_energy

    def predict_on_structure_par(self):
        n = 0
        with concurrent.futures.ProcessPoolExecutor() as executor:
            for res in executor.map(self.predict_on_atom, self.atom_list):
                for i in range(3):
                    self.structure.forces[n][i] = res[0][i]
                    self.structure.stds[n][i] = res[1][i]
                self.local_energies[n] = res[2]
                n += 1
        self.structure.dft_forces = False

    def predict_on_structure(self):
        for n in range(self.structure.nat):
            chemenv = ChemicalEnvironment(self.structure, n)
            for i in range(3):
                force, var = self.gp.predict(chemenv, i + 1)
                self.structure.forces[n][i] = float(force)
                self.structure.stds[n][i] = np.sqrt(np.absolute(var))
            self.local_energies[n] = self.gp.predict_local_energy(chemenv)

        self.structure.dft_forces = False

    def run_and_train(self, target_atom: int = None):
        self.write_to_output('=' * 20 + '\n')

        if target_atom is None and self.parsimony is False:
            self.write_to_output('Calling Quantum Espresso...')
        elif target_atom is None and self.parsimony is True:
            train_atoms = self.init_atoms
            self.write_to_output('Calling DFT with training atoms {}...'
                                 .format(train_atoms))
        else:
            self.write_to_output('Calling DFT due to atom {} at position {} '
                                 'with uncertainties {}...'
                                 .format(target_atom,
                                         self.structure.positions[target_atom],
                                         self.structure.stds[target_atom]))
            train_atoms = [target_atom]

        if self.parsimony is False:
            train_atoms = list(range(self.structure.nat))

        forces = run_espresso(self.qe_input, self.structure,
                              self.pw_loc)
        self.structure.forces = forces
        self.structure.stds = [np.zeros(3) for n in range(self.structure.nat)]
        self.structure.dft_forces = True

        self.write_to_output('Done.\n')
        time_curr = time.time() - self.start_time
        self.write_to_output('wall time from start: %.2f s \n' % time_curr)

        self.write_to_output('Updating database...\n')
        self.gp.update_db(self.structure, forces,
                          custom_range=train_atoms)
        if self.freeze_hyps is False:
            self.gp.train()
        else:
            self.gp.set_L_alpha()
        self.write_hyps()

    def update_positions(self):
        dtdt = self.dt ** 2

        for i, pre_pos in enumerate(self.structure.prev_positions):
            temp_pos = np.copy(self.structure.positions[i])
            mass = self.structure.mass_dict[self.structure.species[i]]
            pos = self.structure.positions[i]
            forces = self.structure.forces[i]

            self.structure.positions[i] = \
                2 * pos - pre_pos + dtdt * forces / mass

            self.structure.prev_positions[i] = np.copy(temp_pos)

        self.velocities = (self.structure.positions -
                           self.structure.prev_positions) / self.dt
        self.curr_step += 1

    @staticmethod
    def write_to_output(string: str, output_file: str = 'otf_run.out'):
        with open(output_file, 'a') as f:
            f.write(string)

    def write_header(self):
        with open('otf_run.out', 'w') as f:
            f.write(str(datetime.datetime.now()) + '\n')

        headerstring = ''
        headerstring += 'Structure Cutoff Radius: {}\n'.format(
            self.structure.cutoff)
        headerstring += 'Kernel: {}\n'.format(
            self.gp.kernel_name)
        headerstring += '# Hyperparameters: {}\n'.format(
            len(self.gp.hyps))
        headerstring += 'Hyperparameter Optimization Algorithm: {}' \
                        '\n'.format(self.gp.algo)
        headerstring += 'Timestep (ps): {}\n'.format(self.dt)
        headerstring += 'Number of Frames: {}\n'.format(self.Nsteps)
        headerstring += 'Number of Atoms: {}\n'.format(self.structure.nat)
        headerstring += 'System Species: {}\n'.format(set(
            self.structure.species))
        headerstring += 'Periodic cell: \n'
        headerstring += str(self.structure.cell)
        headerstring += '\n'

        self.write_to_output(headerstring)

    def write_md_config(self):
        string = "-------------------- \n"

        # Mark if a frame had DFT forces with an asterisk
        string += "-" + ('*' if self.structure.dft_forces else ' ') + \
                  "Frame: " + str(self.curr_step)

        string += ' Simulation Time: %.3f ps \n' % (self.dt * self.curr_step)

        # Construct Header line
        string += 'El \t\t\t  Position (A) \t\t\t\t\t '
        if not self.structure.dft_forces:
            string += 'GP Force (ev/A) '
        else:
            string += 'DFT Force (ev/A) '
        string += '\t\t\t\t\t\t Std. Dev (ev/A)'
        string += '\t\t\t\t\t\t Velocity (A/ps) \n'

        # Construct atom-by-atom description
        KE = 0
        for i in range(len(self.structure.positions)):
            string += self.structure.species[i] + ' '
            for j in range(3):
                string += str("%.8f" % self.structure.positions[i][j]) + ' '
            string += '\t'
            for j in range(3):
                string += str("%.8f" % self.structure.forces[i][j]) + ' '
            string += '\t'
            for j in range(3):
                string += str("%.6e" % self.structure.stds[i][j]) + ' '
            string += '\t'
            for j in range(3):
                string += str("%.6e" % self.velocities[i][j]) + ' '
                KE += 0.5 * \
                    self.structure.mass_dict[self.structure.species[i]] * \
                    self.velocities[i][j] * self.velocities[i][j]
            string += '\n'

        kb = 0.0000861733034
        temperature = 2 * KE / (3 * (len(self.structure.positions)-1) * kb)
        pot_en = np.sum(self.local_energies)
        tot_en = KE + pot_en

        string += 'temperature: %.2f K \n' % temperature
        string += 'kinetic energy: %.6f eV \n' % KE
        string += 'potential energy (up to a constant): %.6f eV \n' % pot_en
        string += 'total energy: %.6f eV \n' % tot_en
        string += 'wall time from start: %.2f s \n' % \
            (time.time() - self.start_time)

        self.write_to_output(string)

    def write_time_info(self, pred_time, std_time, write_time, update_time):
        string = 'prediction time: %.3e s \n' % pred_time
        string += 'std time: %.3e s \n' % std_time
        string += 'write time: %.3e s \n' % write_time
        string += 'update time: %.3e s \n' % update_time

        self.write_to_output(string)

    def write_hyps(self):
        self.write_to_output('New GP Hyperparameters: \n')

        for i, label in enumerate(self.gp.hyp_labels):
            self.write_to_output('Hyp{} : {} = {}\n'
                                 .format(i, label, self.gp.hyps[i]))
        time_curr = time.time() - self.start_time
        self.write_to_output('wall time from start: %.2f s \n' % time_curr)
        self.write_to_output('number of DFT calls: %i \n' % self.dft_count)

    def conclude_run(self):
        footer = '▬' * 20 + '\n'
        footer += 'Run complete. \n'

        self.write_to_output(footer)

    def is_std_in_bound(self) -> (bool, int):
        stds = self.structure.stds
        max_std = np.nanmax(stds)

        # negative tolerance is a hard user-defined std cutoff
        # positive is a multiple of the noise std
        # 0 means no failure condition (DFT is off)
        tol = self.std_tolerance

        if tol == 0:
            return True, -1
        constant_cutoff_trip = tol < 0 and max_std >= np.abs(tol)
        rel_cutoff_trip = tol > 0 and max_std >= tol * np.abs(self.gp.hyps[-1])

        if constant_cutoff_trip or rel_cutoff_trip:
            nat = self.structure.nat
            target_atom = np.argmax([np.max(stds[i]) for i in range(nat)])
            return False, target_atom
        else:
            return True, -1
