#!/usr/bin/env python
# vim: set fileencoding=utf-8 :
# Tiago de Freitas Pereira <tiago.pereira@idiap.ch>

"""

Implementing the algorithm Geodesic Flow Kernel to do transfer learning from the modality A to modality B from the paper
Gong, Boqing, et al. "Geodesic flow kernel for unsupervised domain adaptation." Computer Vision and Pattern Recognition (CVPR), 2012 IEEE Conference on. IEEE, 2012.

A very good explanation can be found here
http://www-scf.usc.edu/~boqinggo/domainadaptation.html#gfk_section

"""

import bob.io.base
import numpy
import numpy.matlib
import scipy.linalg

import logging
logger = logging.getLogger("bob.learn.linear")


def null_space(A, eps=1e-20):
    """
    Computes the left null space of `A`.
    The left null space of A is the orthogonal complement to the column space of A.

    """
    U, S, V = scipy.linalg.svd(A)

    padding = max(0, numpy.shape(A)[1] - numpy.shape(S)[0])
    null_mask = numpy.concatenate(((S <= eps), numpy.ones((padding,), dtype=bool)), axis=0)
    null_s = scipy.compress(null_mask, V, axis=0)
    return scipy.transpose(null_s)


class GFKMachine(object):
    """
    GFK Machine
    """

    def __init__(self, hdf5=None):
        self.source_machine = None
        self.target_machine = None
        self.K = None

        if isinstance(hdf5, bob.io.base.HDF5File):
            self.load(hdf5)

    def load(self, hdf5):
        """
        Loads the machine from the given HDF5 file

        **Parameters**

          hdf5: `bob.io.base.HDF5File`
            An HDF5 file opened for reading

        """

        assert isinstance(hdf5, bob.io.base.HDF5File)

        # read PCA projector
        hdf5.cd("/source_machine")
        self.source_machine = bob.learn.linear.Machine(hdf5)
        hdf5.cd("..")
        hdf5.cd("/target_machine")
        self.target_machine = bob.learn.linear.Machine(hdf5)
        hdf5.cd("..")
        self.K = hdf5.get("K")

    def save(self, hdf5):
        """
        Saves the machine to the given HDF5 file

        **Parameters**

          hdf5: `bob.io.base.HDF5File`
            An HDF5 file opened for reading
        """

        hdf5.create_group("source_machine")
        hdf5.cd("/source_machine")
        self.source_machine.save(hdf5)
        hdf5.cd("..")
        hdf5.create_group("target_machine")
        hdf5.cd("/target_machine")
        self.target_machine.save(hdf5)
        hdf5.cd("..")
        hdf5.set("K", self.K)

    def shape(self):
        """
        A tuple that represents the shape of the kernel matrix

        **Returns**
         (int, int) <– The size of the weights matrix
        """
        return self.G.shape

    def compute_principal_angles(self):
        """
        Compute the principal angles between source (:math:`P_s`) and target (:math:`P_t`) subspaces in a Grassman which is defined as the following:

        :math:`d^{2}(P_s, P_t) = \sum_{i}(\theta_{i}^{2})`,

        """
        Ps = self.source_machine.weights
        Pt = self.target_machine.weights

        # S = cos(theta_1, theta_2, ..., theta_n)
        _, S, _ = numpy.linalg.svd(numpy.dot(Ps.T, Pt))
        thetas_squared = numpy.arccos(S) ** 2

        return numpy.sum(thetas_squared)

    def compute_binetcouchy_distance(self):
        """
        Compute the Binet-Couchy distance between source (:math:`P_s`) and target (:math:`P_t`) subspaces in a Grassman which is defined as the following:

        :math:`d(P_s, P_t) = 1 - (det(P_{s}^{T} * P_{t}^{T}))^{2}`
        """

        # Preparing the source
        Ps = self.source_machine.weights
        Rs = self.space(Ps.T)
        Y1 = numpy.hstack((Ps, Rs))

        # Preraring the target
        Pt = self.target_machine.weights
        Rt = null_space(Pt.T)
        Y2 = numpy.hstack((Pt, Rt))

        return 1 - numpy.linalg.det(numpy.dot(Y1.T, Y2)) ** 2

    def __call__(self, source_domain_data, target_domain_data):
        """
        Compute dot product in the infinity space

        **Parameters**

        source_domain_data:
        target_domain_data:
        """

        source_domain_data = (source_domain_data - self.source_machine.input_subtract) / self.source_machine.input_divide
        target_domain_data = (target_domain_data - self.target_machine.input_subtract) / self.target_machine.input_divide

        return numpy.dot(numpy.dot(source_domain_data, self.G), target_domain_data.T)[0]


class GFKTrainer(object):
    """
    GFK Trainer
    """

    def __init__(self, principal_angles_dimension, subspace_dim_source=0.99, subspace_dim_target=0.99, eps=1e-20):
        self.m_principal_angles_dimension = principal_angles_dimension
        self.m_subspace_dim_source = subspace_dim_source
        self.m_subspace_dim_target = subspace_dim_target
        self.eps = eps

    def train(self, source_data, target_data):

        source_data = source_data.astype("float64")
        target_data = target_data.astype("float64")

        logger.info("  -> Normalizing data per modality")
        source, mu_source, std_source = self._znorm(source_data)
        target, mu_target, std_target = self._znorm(target_data)

        logger.info("  -> Computing PCA for the source modality")
        Ps = self._train_pca(source, mu_source, std_source, self.m_subspace_dim_source)
        logger.info("  -> Computing PCA for the target modality")
        Pt = self._train_pca(target, mu_target, std_target, self.m_subspace_dim_target)
        # self.m_machine                = bob.io.base.load("/idiap/user/tpereira/gitlab/workspace_HTFace/GFK.hdf5")

        K = self._train_gfk(numpy.hstack((Ps.weights, null_space(Ps.weights.T))),
                            Pt.weights[:, 0:self.m_principal_angles_dimension])

        machine = GFKMachine()
        machine.source_machine = Ps
        machine.target_machine = Pt
        machine.K = K

        return machine

    def _train_gfk(self, Ps, Pt):
        """
        Trains GFK
        """

        N = Ps.shape[1]
        dim = Pt.shape[1]

        # Principal angles between subspaces
        QPt = numpy.dot(Ps.T, Pt)

        # [V1,V2,V,Gam,Sig] = gsvd(QPt(1:dim,:), QPt(dim+1:end,:));
        A = QPt[0:dim, :].copy()
        B = QPt[dim:, :].copy()

        # Equation (2)
        [V1, V2, V, Gam, Sig] = bob.math.gsvd(A, B)
        V2 = -V2

        # Some sanity checks with the GSVD
        I = numpy.eye(V1.shape[1])
        I_check = numpy.dot(Gam.T, Gam) + numpy.dot(Sig.T, Sig)
        assert numpy.sum(abs(I - I_check)) < 1e-10

        theta = numpy.arccos(numpy.diagonal(Gam))

        # Equation (6)
        B1 = numpy.diag(0.5 * (1 + (numpy.sin(2 * theta) / (2. * numpy.maximum
        (theta, self.eps)))))
        B2 = numpy.diag(0.5 * ((numpy.cos(2 * theta) - 1) / (2 * numpy.maximum(
            theta, self.eps))))
        B3 = B2
        B4 = numpy.diag(0.5 * (1 - (numpy.sin(2 * theta) / (2. * numpy.maximum
        (theta, self.eps)))))

        # Equation (9) of the suplementary matetial
        delta1_1 = numpy.hstack((V1, numpy.zeros(shape=(dim, N - dim))))
        delta1_2 = numpy.hstack((numpy.zeros(shape=(N - dim, dim)), V2))
        delta1 = numpy.vstack((delta1_1, delta1_2))

        delta2_1 = numpy.hstack((B1, B2, numpy.zeros(shape=(dim, N - 2 * dim))))
        delta2_2 = numpy.hstack((B3, B4, numpy.zeros(shape=(dim, N - 2 * dim))))
        delta2_3 = numpy.zeros(shape=(N - 2 * dim, N))
        delta2 = numpy.vstack((delta2_1, delta2_2, delta2_3))

        delta3_1 = numpy.hstack((V1, numpy.zeros(shape=(dim, N - dim))))
        delta3_2 = numpy.hstack((numpy.zeros(shape=(N - dim, dim)), V2))
        delta3 = numpy.vstack((delta3_1, delta3_2)).T

        delta = numpy.dot(numpy.dot(delta1, delta2), delta3)
        K = numpy.dot(numpy.dot(Ps, delta), Ps.T)

        return K

    def _train_pca(self, data, mu_data, std_data, subspace_dim):
        t = bob.learn.linear.PCATrainer()
        machine, variances = t.train(data)

        # For re-shaping, we need to copy...
        variances = variances.copy()

        # compute variance percentage, if desired
        if isinstance(subspace_dim, float):
            cummulated = numpy.cumsum(variances) / numpy.sum(variances)
            for index in range(len(cummulated)):
                if cummulated[index] > subspace_dim:
                    subspace_dim = index
                    break
            subspace_dim = index
        logger.info("    ... Keeping %d PCA dimensions", subspace_dim)

        machine.resize(machine.shape[0], subspace_dim)
        machine.input_subtract = mu_data
        machine.input_divide = std_data

        return machine

    def _znorm(self, data):
        """
        Z-Normaliza
        """

        mu = numpy.average(data, axis=0)
        std = numpy.std(data, axis=0)

        data = (data - mu) / std

        return data, mu, std


