"""Tigramite causal discovery for time series."""

# Author: Jakob Runge <jakobrunge@posteo.de>
#
# License: GNU General Public License v3.0

import warnings
import numpy
import sys
import math

from scipy import linalg, special, stats

try:
    from sklearn import gaussian_process
except:
    print("Could not import sklearn for GPACE")

try:
    import rpy2
    import rpy2.robjects
    rpy2.robjects.r['options'](warn=-1)

    from rpy2.robjects.packages import importr
    acepack = importr('acepack')
    import rpy2.robjects.numpy2ri
    rpy2.robjects.numpy2ri.activate()

#     def warn(*args, **kwargs):
#         pass
#     import warnings
#     warnings.warn = warn
except:
    print("Could not import rpy acepack package for GPACE,"
          " use python ACE package")

try: 
    import ace
except:
    print("Could not import python ACE package for GPACE")

try:
    from scipy import spatial
    import tigramite_cython_code
except:
    print("Could not import packages for knn-CMI estimation")


class CondIndTest(object):
    """Base class of conditional independence tests.

    Provides useful general functions for different independence tests such as
    shuffle significance testing and bootstrap confidence estimation. Also
    handles masked samples. Other test classes can inherit from this class.

    Parameters
    ----------
    use_mask : bool, optional (default: False)
        Whether a supplied mask should be used.

    mask_type : list including all or some of the strings 'x', 'y', 'z',
        optional (default: None) 
        Marks for which variables in the dependence measure I(X; Y | Z) the
        samples should be masked. If None, ['x', 'y', 'z'] is used, which
        excludes masked samples in X, Y, and Z and can be used for missing
        values. Explained in [1]_.

    significance : str, optional (default: 'analytic')
        Type of significance test to use. In this package 'analytic', 
        'fixed_thres' and 'shuffle_test' are available.

    fixed_thres : float, optional (default: 0.1)
        If significance is 'fixed_thres', this specifies the threshold for the 
        absolute value of the dependence measure.

    sig_samples : int, optional (default: 100)
        Number of samples for shuffle significance test. 

    sig_blocklength : int, optional (default: None)
        Block length for block-shuffle significance test. If None, the
        block length is determined from the decay of the autocovariance as 
        explained in [1]_.

    confidence : False or str, optional (default: False)
        Specify type of confidence estimation. If False, numpy.nan is returned.
        'bootstrap' can be used with any test, for ParCorr also 'analytic' is
        implemented.

    conf_lev : float, optional (default: 0.9)
        Two-sided confidence interval.

    conf_samples : int, optional (default: 100)
        Number of samples for bootstrap.

    conf_blocklength : int, optional (default: None)
        Block length for block-bootstrap. If None, the block length is
        determined from the decay of the autocovariance as explained in [1]_.

    recycle_residuals : bool, optional (default: False)
        Specifies whether residuals should be stored. This may be faster, but
        can cost considerable memory.

    verbosity : int, optional (default: 0)
        Level of verbosity.
    """

    def __init__(self, 
        use_mask=False,
        mask_type=None,

        significance='analytic',
        fixed_thres=0.1,
        sig_samples=100,
        sig_blocklength=None,

        confidence=False,
        conf_lev=0.9,
        conf_samples=100,
        conf_blocklength=None,

        recycle_residuals=False,
        verbosity=0
        ):

        self.use_mask = use_mask
        self.mask_type = mask_type
        self.significance = significance
        self.sig_samples = sig_samples
        self.sig_blocklength = sig_blocklength
        self.fixed_thres = fixed_thres

        self.confidence = confidence
        self.conf_lev = conf_lev
        self.conf_samples = conf_samples
        self.conf_blocklength = conf_blocklength

        self.verbosity = verbosity

        self.recycle_residuals = recycle_residuals
        if self.recycle_residuals:
            self.residuals = {}

        if self.use_mask:
            self.recycle_residuals = False
        if self.mask_type is None:
            self.mask_type = ['x', 'y', 'z']

        if use_mask:
            if mask_type is None or len(set(mask_type) -
                                        set(['x', 'y', 'z'])) > 0:
                raise ValueError("mask_type = %s, but must be list containing"
                                 % mask_type + " 'x','y','z', or any "
                                 "combination")

    def set_dataframe(self, dataframe):
        """Initialize dataframe.

        Parameters
        ----------
        dataframe : data object
            This can either be the tigramite dataframe object or a pandas data
            frame. It must have the attributes dataframe.values yielding a numpy
            array of shape (observations T, variables N) and optionally a mask
            of the same shape.

        """
        self.data = dataframe.values
        self.mask = dataframe.mask

    def _keyfy(self, x, z):
        """Helper function to make lists unique."""
        return (tuple(set(x)), tuple(set(z)))

    def _get_array(self, X, Y, Z, tau_max=0, verbosity=None):
        """Convencience wrapper around _construct_array."""
        
        if verbosity is None:
            verbosity=self.verbosity

        return self._construct_array(
            X=X, Y=Y, Z=Z,
            tau_max=tau_max,
            data=self.data,
            use_mask=self.use_mask,
            mask=self.mask,
            mask_type=self.mask_type,
            return_cleaned_xyz=True,
            do_checks=False,
            verbosity=verbosity)

    # @profile
    def _construct_array(self, X, Y, Z, tau_max, data,
                         use_mask=False,
                         mask=None, mask_type=None,
                         return_cleaned_xyz=False,
                         do_checks=True,
                         verbosity=0):
        """Constructs array from variables X, Y, Z from data.

        Data is of shape (T, N), where T is the time series length and N the
        number of variables.

        Parameters
        ----------
        X, Y, Z : list of tuples
            For a dependence measure I(X;Y|Z), Y is of the form [(varY, 0)],
            where var specifies the variable index. X typically is of the form
            [(varX, -tau)] with tau denoting the time lag and Z can be
            multivariate [(var1, -lag), (var2, -lag), ...] .

        tau_max : int
            Maximum time lag. This may be used to make sure that estimates for
            different lags in X and Z all have the same sample size.

        data : array-like, 
            This is the data input array of shape = (T, N)

        use_mask : bool, optional (default: False)
            Whether a supplied mask should be used.

        mask : boolean array, optional (default: False)
            Mask of data array, marking masked values as 1. Must be of same
            shape as data.

        mask_type : list, optional (default: False)
            Can including all or some of the strings {'x', 'y', 'z'}. Marks for
            which variables in the dependence measure I(X; Y | Z) the samples
            should be masked. If None, ['x', 'y', 'z'] is used, which excludes
            masked samples in X, Y, and Z and can be used for missing values.
            See [1]_ for further discussion.

        return_cleaned_xyz : bool, optional (default: False)
            Whether to return cleaned X,Y,Z, where possible duplicates are 
            removed.

        do_checks : bool, optional (default: True)
            Whether to perform sanity checks on input X,Y,Z

        verbosity : int, optional (default: 0)
            Level of verbosity.

        Returns
        -------
        array, xyz [,XYZ] : Tuple of data array of shape (dim, T) and xyz 
            identifier array of shape (dim,) identifying which row in array
            corresponds to X, Y, and Z. For example::
                X = [(0, -1)], Y = [(1, 0)], Z = [(1, -1), (0, -2)]
                yields an array of shape (5, T) and xyz is  
                xyz = numpy.array([0,1,2,2])          
            If return_cleaned_xyz is True, also outputs the cleaned XYZ lists.
         
        """

        def uniq(input):
            output = []
            for x in input:
                if x not in output:
                    output.append(x)
            return output

        data_type = data.dtype

        T, N = data.shape

        # Remove duplicates in X, Y, Z
        X = uniq(X)
        Y = uniq(Y)
        Z = uniq(Z)

        if do_checks:
            if len(X) == 0:
                raise ValueError("X must be non-zero")
            if len(Y) == 0:
                raise ValueError("Y must be non-zero")

        # If a node in Z occurs already in X or Y, remove it from Z
        Z = [node for node in Z if (node not in X) and (node not in Y)]

        # Check that all lags are non-positive and indices are in [0,N-1]
        XYZ = X + Y + Z
        dim = len(XYZ)

        if do_checks:
            if numpy.array(XYZ).shape != (dim, 2):
                raise ValueError("X, Y, Z must be lists of tuples in format"
                                 " [(var, -lag),...], eg., [(2, -2), (1, 0), ...]")
            if numpy.any(numpy.array(XYZ)[:, 1] > 0):
                raise ValueError("nodes are %s, " % str(XYZ) +
                                 "but all lags must be non-positive")
            if (numpy.any(numpy.array(XYZ)[:, 0] >= N)
                    or numpy.any(numpy.array(XYZ)[:, 0] < 0)):
                raise ValueError("var indices %s," % str(numpy.array(XYZ)[:, 0]) +
                                 " but must be in [0, %d]" % (N - 1))
            if numpy.all(numpy.array(Y)[:, 1] < 0):
                raise ValueError("Y-nodes are %s, " % str(Y) +
                                 "but one of the Y-nodes must have zero lag")

        max_lag = max(abs(numpy.array(XYZ)[:, 1].min()), tau_max)

        # Setup XYZ identifier
        xyz = numpy.array([0 for i in range(len(X))] +
                          [1 for i in range(len(Y))] +
                          [2 for i in range(len(Z))])

        # Setup and fill array with lagged time series
        array = numpy.zeros((dim, T - max_lag), dtype=data_type)
        for i, node in enumerate(XYZ):
            var, lag = node
            array[i, :] = data[max_lag + lag: T + lag, var]

        if use_mask:
            # Remove samples with mask == 1
            # conditional on which mask_type is used
            array_selector = numpy.zeros((dim, T - max_lag), dtype='int32')
            for i, node in enumerate(XYZ):
                var, lag = node
                array_selector[i, :] = (
                    mask[max_lag + lag: T + lag, var] == False)

            use_indices = numpy.ones(T - max_lag, dtype='int')
            if 'x' in mask_type:
                use_indices *= numpy.prod(array_selector[xyz == 0, :], axis=0)
            if 'y' in mask_type:
                use_indices *= numpy.prod(array_selector[xyz == 1, :], axis=0)
            if 'z' in mask_type:
                use_indices *= numpy.prod(array_selector[xyz == 2, :], axis=0)

            if use_indices.sum() == 0:
                raise ValueError("No unmasked samples")

            array = array[:, use_indices == 1]

        if verbosity > 2:
            print("            Constructed array of shape " +
                  "%s from\n" % str(array.shape) +
                  "            X = %s\n" % str(X) +
                  "            Y = %s\n" % str(Y) +
                  "            Z = %s" % str(Z))
            if use_mask:
                print("            with masked samples in "
                      "%s removed" % str(mask_type))

        if return_cleaned_xyz:
            return array, xyz, (X, Y, Z)
        else:
            return array, xyz

    # @profile
    def run_test(self, X, Y, Z=None, tau_max=0):
        """Perform conditional independence test.

        Calls the dependence measure and signficicance test functions. The child
        classes must specify a function get_dependence_measure and either or
        both functions get_analytic_significance and  get_shuffle_significance.
        If recycle_residuals is True, also  _get_single_residuals must be
        available.

        Parameters
        ----------
        X, Y, Z : list of tuples
            X,Y,Z are of the form [(var, -tau)], where var specifies the 
            variable index and tau the time lag.

        tau_max : int, optional (default: 0)
            Maximum time lag. This may be used to make sure that estimates for
            different lags in X, Z, all have the same sample size.

        Returns
        -------
        val, pval : Tuple of floats
        
            The test statistic value and the p-value. These are also made in the
            class as self.val and self.pval.
        
        """

        array, xyz, XYZ = self._get_array(X, Y, Z, tau_max)
        X, Y, Z = XYZ

        D, T = array.shape

        if numpy.isnan(array).sum() != 0:
            raise ValueError("nans in the array!")

        if self.recycle_residuals:
            if self._keyfy(X, Z) in self.residuals.keys():
                x_resid = self.residuals[self._keyfy(X, Z)]
            else:
                x_resid = self._get_single_residuals(array, target_var = 0)
                if len(Z) > 0:
                    self.residuals[self._keyfy(X, Z)] = x_resid

            if self._keyfy(Y, Z) in self.residuals.keys():
                y_resid = self.residuals[self._keyfy(Y, Z)]
            else:
                y_resid = self._get_single_residuals(array,target_var = 1)
                if len(Z) > 0:
                    self.residuals[self._keyfy(Y, Z)] = y_resid

            array_resid = numpy.array([x_resid, y_resid])
            xyz_resid = numpy.array([0, 1])

            val = self.get_dependence_measure(array_resid, xyz_resid)

        else:
            val = self.get_dependence_measure(array, xyz)

        if self.significance == 'analytic':
            pval = self.get_analytic_significance(value=val, df=T-D)

        elif self.significance == 'shuffle_test':
            pval = self.get_shuffle_significance(array=array,
                                                 xyz=xyz,
                                                 value=val)
        elif self.significance == 'fixed_thres':
            pval = get_fixed_thres_significance(value=val, 
                                                fixed_thres=self.fixed_thres)
        else:
            raise ValueError("%s not known." % self.significance)

        self.X = X
        self.Y = Y
        self.Z = Z
        self.val = val
        self.pval = pval

        return val, pval

    def get_measure(self, X, Y, Z=None, tau_max=0):
        """Estimate dependence measure.

        Calls the dependence measure function. The child classes must specify
        a function get_dependence_measure.

        Parameters
        ----------
        X, Y [, Z] : list of tuples
            X,Y,Z are of the form [(var, -tau)], where var specifies the 
            variable index and tau the time lag.

        tau_max : int, optional (default: 0)
            Maximum time lag. This may be used to make sure that estimates for
            different lags in X, Z, all have the same sample size.

        Returns
        -------
        val : float
            The test statistic value.
        
        """

        array, xyz, XYZ = self._get_array(X, Y, Z, tau_max)
        X, Y, Z = XYZ

        D, T = array.shape

        if numpy.isnan(array).sum() != 0:
            raise ValueError("nans in the array!")

        if self.recycle_residuals:
            if self._keyfy(X, Z) in self.residuals.keys():
                x_resid = self.residuals[self._keyfy(X, Z)]
            else:
                x_resid = self._get_single_residuals(array, target_var = 0)
                if len(Z) > 0:
                    self.residuals[self._keyfy(X, Z)] = x_resid

            if self._keyfy(Y, Z) in self.residuals.keys():
                y_resid = self.residuals[self._keyfy(Y, Z)]
            else:
                y_resid = self._get_single_residuals(array,target_var = 1)
                if len(Z) > 0:
                    self.residuals[self._keyfy(Y, Z)] = y_resid

            array_resid = numpy.array([x_resid, y_resid])
            xyz_resid = numpy.array([0, 1])

            val = self.get_dependence_measure(array_resid, xyz_resid)

        else:
            val = self.get_dependence_measure(array, xyz)

        return val

    def get_confidence(self, X, Y, Z=None, tau_max=0):
        """Perform confidence interval estimation.

        Calls the dependence measure and confidence test functions. The child
        classes can specify a function get_dependence_measure and
        get_analytic_confidence or get_bootstrap_confidence. If confidence is
        False, (numpy.nan, numpy.nan) is returned.

        Parameters
        ----------
        X, Y, Z : list of tuples
            X,Y,Z are of the form [(var, -tau)], where var specifies the 
            variable index and tau the time lag.

        tau_max : int, optional (default: 0)
            Maximum time lag. This may be used to make sure that estimates for
            different lags in X, Z, all have the same sample size.

        Returns
        -------
        (conf_lower, conf_upper) : Tuple of floats
            Upper and lower confidence bound of confidence interval.
        """

        if self.confidence:
            if (self.conf_lev < .5 or self.conf_lev >= 1.):
                raise ValueError("conf_lev = %.2f, " % self.conf_lev +
                                 "but must be between 0.5 and 1")
            if (self.confidence == 'bootstrap'
                    and self.conf_samples * (1. - self.conf_lev) / 2. < 1.):
                raise ValueError("conf_samples*(1.-conf_lev)/2 is %.2f"
                                 % (self.conf_samples * (1. - self.conf_lev) / 2.) +
                                 ", must be >> 1")

        array, xyz, XYZ = self._get_array(X, Y, Z, tau_max, verbosity=0)

        dim, T = array.shape

        if numpy.isnan(array).sum() != 0:
            raise ValueError("nans in the array!")

        if self.confidence == 'analytic':
            val = self.get_dependence_measure(array, xyz)

            (conf_lower, conf_upper) = self.get_analytic_confidence(df=T-dim, 
                                    value=val, conf_lev=self.conf_lev)

        elif self.confidence == 'bootstrap':
            # Overwrite analytic values
            (conf_lower, conf_upper) = self.get_bootstrap_confidence(array, xyz,
                             dependence_measure=self.get_dependence_measure,
                             conf_samples=self.conf_samples, 
                             conf_blocklength=self.conf_blocklength,
                             conf_lev=self.conf_lev, verbosity=self.verbosity)
        elif self.confidence == False:
            return (numpy.nan, numpy.nan)

        else:
            raise ValueError("%s confidence estimation not implemented" 
                             % self.confidence)

        self.conf = (conf_lower, conf_upper)

        return (conf_lower, conf_upper)

    def _print_cond_ind_results(self, val, pval=None, conf=None):
        """Print results from conditional independence test.

        Parameters
        ----------
        val : float
            Test stastistic value.

        pval : float, optional (default: None)
            p-value

        conf : tuple of floats, optional (default: None)
            Confidence bounds.
        """

        if pval is not None:
            printstr = "        pval = %.5f | val = %.3f" % (
                pval, val)
            if conf is not None:
                printstr += " | conf bounds = (%.3f, %.3f)" % (
                    conf[0], conf[1])
        else:
            printstr = "        val = %.3f" % val
            if conf is not None:
                printstr += " | conf bounds = (%.3f, %.3f)" % (
                    conf[0], conf[1])


        print(printstr)

    # @profile
    def get_bootstrap_confidence(self, array, xyz, dependence_measure,
                             conf_samples=100, conf_blocklength=None,
                             conf_lev=.95, verbosity=0):
        """Perform bootstrap confidence interval estimation.

        With conf_blocklength > 1 or None a block-bootstrap is performed.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).

        dependence_measure : object
            Dependence measure function must be of form 
            dependence_measure(array, xyz) and return a numeric value

        conf_lev : float, optional (default: 0.9)
            Two-sided confidence interval.

        conf_samples : int, optional (default: 100)
            Number of samples for bootstrap.

        conf_blocklength : int, optional (default: None)
            Block length for block-bootstrap. If None, the block length is
            determined from the decay of the autocovariance as explained in
            [1]_.

        verbosity : int, optional (default: 0)
            Level of verbosity.

        Returns
        -------
        (conf_lower, conf_upper) : Tuple of floats
            Upper and lower confidence bound of confidence interval.
        """

        # confidence interval is two-sided
        c_int = (1. - (1. - conf_lev) / 2.)
        dim, T = array.shape

        if conf_blocklength is None:
            conf_blocklength = self._get_block_length(array, xyz,
                                                     mode='confidence')

        n_blocks = int(math.ceil(float(T) / float(conf_blocklength)))

        if verbosity > 2:
            print("            block_bootstrap confidence intervals"
                  " with block-length = %d ..." % conf_blocklength)

        bootdist = numpy.zeros(conf_samples)
        for sam in range(conf_samples):
            rand_block_starts = numpy.random.randint(0,
                         T - conf_blocklength + 1, n_blocks)
            array_bootstrap = numpy.zeros((dim, n_blocks*conf_blocklength), 
                                          dtype = array.dtype)

            # array_bootstrap = array[:, rand_block_starts]
            for b in range(conf_blocklength):
                array_bootstrap[:, b::conf_blocklength] = array[:, 
                                                          rand_block_starts + b]

            # Cut to proper length
            array_bootstrap = array_bootstrap[:, :T]
            bootdist[sam] = dependence_measure(array_bootstrap, xyz)

        # Sort and get quantile
        bootdist.sort()
        conf_lower = bootdist[int((1. - c_int) * conf_samples)]
        conf_upper = bootdist[int(c_int * conf_samples)]

        return (conf_lower, conf_upper)

    def get_shuffle_significance(self, array, xyz, value):
        """Returns p-value for shuffle significance test.

        For residual-based test statistics only the residuals are shuffled.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).

        value : number
            Value of test statistic for unshuffled estimate.
        
        Returns
        -------
        pval : float
            p-value
        """

        if self.residual_based:
            x = self._get_single_residuals(array, target_var = 0)
            y = self._get_single_residuals(array, target_var = 1)
            array = numpy.array([x, y])
            xyz = numpy.array([0,1])

        null_dist = self._get_shuffle_dist(array, xyz,
                               self.get_dependence_measure,
                               sig_samples=self.sig_samples, 
                               sig_blocklength=self.sig_blocklength,
                               verbosity=self.verbosity)

        pval = (null_dist >= numpy.abs(value)).mean()
        if self.two_sided:
            # Adjust p-value for two-sided measures
            if pval < 1.: pval *= 2.

        return pval

    def _get_acf(self, series, max_lag=None):
        """Returns autocorrelation function.
        
        Parameters
        ----------
        series : 1D-array
            data series to compute autocorrelation from

        max_lag : int, optional (default: None)
            maximum lag for autocorrelation function. If None is passed, 10% of
            the data series length are used.
        
        Returns
        -------
        autocorr : array of shape (max_lag + 1,) 
            Autocorrelation function.
        """
        if max_lag is None:
            max_lag = max(5, len(series) / 10)

        autocorr = numpy.ones(max_lag + 1)
        for lag in range(1, max_lag + 1):

            y1 = series[lag:]
            y2 = series[:len(series) - lag]

            autocorr[lag] = numpy.corrcoef(y1, y2, ddof=0)[0, 1]

        return autocorr

    def _get_block_length(self, array, xyz, mode):
        """Returns optimal block length for significance and confidence tests.

        Determine block length using approach in Mader (2013) [Eq. (6)] which
        improves the method of Pfeifer (2005) with non-overlapping blocks In
        case of multidimensional X, the max is used. Further details in [1]_.
        Two modes are available. For mode='significance', only the indices
        corresponding to X are shuffled in array. For  mode='confidence' all
        variables are jointly shuffled. If the autocorrelation curve fit fails,
        a block length of 5% of T is used. The block length is limited to a
        maximum of 10% of T.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).
        
        mode : str
            Which mode to use.
        
        Returns
        -------
        block_len : int
            Optimal block length.
        """

        from scipy import signal, optimize

        dim, T = array.shape

        if mode == 'significance':
            indices = numpy.where(xyz == 0)[0]
        else:
            indices = range(dim)

        # Maximum lag for autocov estimation
        max_lag = T / 10

        def func(x, a, decay):
            return a * decay**x

        block_len = 1
        for i in indices:

            # Get decay rate of envelope of autocorrelation functions
            # via hilbert trafo
            autocov = self._get_acf(series=array[i], max_lag=max_lag)

            autocov[0] = 1.
            hilbert = numpy.abs(signal.hilbert(autocov))

            try:
                popt, pcov = optimize.curve_fit(
                    func, range(0, max_lag + 1), hilbert)
                phi = popt[1]

                # Formula of Pfeifer (2005) assuming non-overlapping blocks
                l_opt = (4. * T * (phi / (1. - phi) + phi**2 / (1. - phi)**2)**2
                         / (1. + 2. * phi / (1. - phi))**2)**(1. / 3.)

                block_len = max(block_len, int(l_opt))

            except RuntimeError:
                print(
                    "Error - curve_fit failed in block_shuffle, using"
                    " block_len = %d" % (int(.05 * T)))
                block_len = max(int(.05 * T), 2)

        # Limit block length to a maximum of 10% of T
        block_len = min(block_len, int(0.1 * T))

        return block_len

    def _get_shuffle_dist(self, array, xyz, dependence_measure,
                          sig_samples, sig_blocklength=None,
                          verbosity=0):
        """Returns shuffle distribution of test statistic.

        The rows in array corresponding to the X-variable are shuffled using
        a block-shuffle approach.


        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).

       dependence_measure : object
           Dependence measure function must be of form 
           dependence_measure(array, xyz) and return a numeric value
 
        sig_samples : int, optional (default: 100)
            Number of samples for shuffle significance test. 

        sig_blocklength : int, optional (default: None)
            Block length for block-shuffle significance test. If None, the
            block length is determined from the decay of the autocovariance as 
            explained in [1]_.

        verbosity : int, optional (default: 0)
            Level of verbosity.
        
        Returns
        -------
        null_dist : array of shape (sig_samples,)
            Contains the sorted test statistic values estimated from the 
            shuffled arrays.
        """

        dim, T = array.shape

        x_indices = numpy.where(xyz == 0)[0]
        dim_x = len(x_indices)

        if sig_blocklength is None:
            sig_blocklength = self._get_block_length(array, xyz,
                                                     mode='significance')

        n_blocks = int(math.floor(float(T) / float(sig_blocklength)))
        # print 'n_blocks ', n_blocks
        if verbosity > 2:
            print("            Significance test with block-length = %d "
                  "..." % (sig_blocklength))

        array_shuffled = numpy.copy(array)
        block_starts = numpy.arange(0, T - sig_blocklength + 1, sig_blocklength)

        # Dividing the array up into n_blocks of length sig_blocklength may
        # leave a tail. This tail is later randomly inserted
        tail = array[x_indices, n_blocks*sig_blocklength:]

        null_dist = numpy.zeros(sig_samples)
        for sam in range(sig_samples):

            rand_block_starts = numpy.random.permutation(block_starts)[:n_blocks]

            x_shuffled = numpy.zeros((dim_x, n_blocks*sig_blocklength), 
                                          dtype = array.dtype)

            for i, index in enumerate(x_indices):
                for b in range(sig_blocklength):
                    x_shuffled[i, b::sig_blocklength] = array[index, 
                                            rand_block_starts + b]

            # Insert tail randomly somewhere
            if tail.shape[1] > 0:
                insert_tail_at = numpy.random.choice(block_starts)
                x_shuffled = numpy.insert(x_shuffled, insert_tail_at, 
                                          tail.T, axis=1)

            for i, index in enumerate(x_indices):
                array_shuffled[index] = x_shuffled[i]

            null_dist[sam] = dependence_measure(array=array_shuffled,
                                           xyz=xyz)

        null_dist.sort()

        return null_dist

    def get_fixed_thres_significance(self, value, fixed_thres):
        """Returns signficance for thresholding test.

        Returns 0 if numpy.abs(value) is smaller than fixed_thres and 1 else.

        Parameters
        ----------
        value : number
            Value of test statistic for unshuffled estimate.

        fixed_thres : number
            Fixed threshold, is made positive.

        Returns
        -------
        pval : bool
            Returns 0 if numpy.abs(value) is smaller than fixed_thres and 1
            else.

        """
        if numpy.abs(value) < numpy.abs(fixed_thres):
            pval = 1.
        else:
            pval = 0.

        return pval

class ParCorr(CondIndTest):
    """Partial correlation test.

    Partial correlation is estimated as described in [1]_. 

    Parameters
    ----------
    **kwargs : 
        Arguments passed on to Parent class CondIndTest.

    """

    def __init__(self, **kwargs):

        # super(ParCorr, self).__init__(
        CondIndTest.__init__(self, **kwargs)

        self.measure = 'par_corr'
        self.two_sided = True
        self.residual_based = True

    # @profile
    def _get_single_residuals(self, array, target_var, 
                standardize = True,
                return_means=False):
        """Returns residuals of linear multiple regression.

        Performs a OLS regression of the variable indexed by target_var on the
        conditions Z. Here array is assumed to contain X and Y as the first two
        rows with the remaining rows (if present) containing the conditions Z.
        Optionally returns the estimated regression line.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        target_var : {0, 1}
            Variable to regress out conditions from.

        standardize : bool, optional (default: True)
            Whether to standardize the array beforehand. Must be used for 
            partial correlation.

        return_means : bool, optional (default: False)
            Whether to return the estimated regression line.

        Returns
        -------
        resid [, mean] : array-like
            The residual of the regression and optionally the estimated line.
        """

        dim, T = array.shape
        dim_z = dim - 2

        # Standardize
        if standardize:
            array -= array.mean(axis=1).reshape(dim, 1)
            array /= array.std(axis=1).reshape(dim, 1)
            if numpy.isnan(array).sum() != 0:
                raise ValueError("nans after standardizing, "
                                 "possibly constant array!")

        y = array[target_var, :]

        if dim_z > 0:
            z = numpy.fastCopyAndTranspose(array[2:, :])
            beta_hat = numpy.linalg.lstsq(z, y)[0]
            mean = numpy.dot(z, beta_hat)
            resid = y - mean
        else:
            resid = y
            mean = None

        if return_means:
            return (resid, mean)
        else:
            return resid

    # @profile
    def get_dependence_measure(self, array, xyz):
        """Return partial correlation.

        Estimated as the Pearson correlation of the residuals of a linear
        OLS regression.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).

        Returns
        -------
        val : float
            Partial correlation coefficient.    
        """

        x = self._get_single_residuals(array, target_var = 0)
        y = self._get_single_residuals(array, target_var = 1)

        val, dummy = stats.pearsonr(x, y)

        return val

    def get_analytic_significance(self, value, df): 
        """Returns analytic p-value from Student's t-test for the Pearson
        correlation coefficient.
        
        Assumes two-sided correlation. If the degrees of freedom are less than
        1, numpy.nan is returned.
        
        Parameters
        ----------
        value : float
            Test statistic value.

        df : int
            degrees of freedom of the test, given by T - dim

        Returns
        -------
        pval : float or numpy.nan
            P-value.
        """

        if df < 1:
            pval = numpy.nan
        else:
            trafo_val = value * numpy.sqrt(df / (1. - value**2))
            # Two sided significance level
            pval = stats.t.sf(numpy.abs(trafo_val), df) * 2

        return pval

    def get_analytic_confidence(self, value, df, conf_lev):
        """Returns analytic confidence interval for correlation coefficient.
        
        Based on Student's t-distribution.

        Parameters
        ----------
        value : float
            Test statistic value.

        df : int
            degrees of freedom of the test, given by T - dim

        conf_lev : float
            Confidence interval, eg, 0.9

        Returns
        -------
        (conf_lower, conf_upper) : Tuple of floats
            Upper and lower confidence bound of confidence interval.
        """

        # Confidence interval is two-sided
        c_int = (1. - (1. - conf_lev) / 2.)

        value_tdist = value * numpy.sqrt(df) / numpy.sqrt(1. - value**2)
        conf_lower = (stats.t.ppf(q=1. - c_int, df=df, loc=value_tdist)
                      / numpy.sqrt(df + stats.t.ppf(q=1. - c_int, df=df,
                                                       loc=value_tdist)**2))
        conf_upper = (stats.t.ppf(q=c_int, df=df, loc=value_tdist)
                      / numpy.sqrt(df + stats.t.ppf(q=c_int, df=df,
                                                       loc=value_tdist)**2))
        return (conf_lower, conf_upper)


    def get_model_selection_criterion(self, j, parents, tau_max=0):
        """Returns Akaike's Information criterion modulo constants.
        
        Fits a linear model of the parents to variable j and returns the score.
        I used to determine optimal hyperparameters in PCMCI, in particular 
        the pc_alpha value.
        
        Parameters
        ----------
        j : int
            Index of target variable in data array.

        parents : list
            List of form [(0, -1), (3, -2), ...] containing parents.

        tau_max : int, optional (default: 0)
            Maximum time lag. This may be used to make sure that estimates for
            different lags in X, Z, all have the same sample size.
        
        Returns:
        score : float
            Model score.
        """

        Y = [(j, 0)]
        X = [(j, 0)]   # dummy variable here
        Z = parents
        array, xyz = self._construct_array(
            X=X, Y=Y, Z=Z,
            tau_max=tau_max,
            data=self.data,
            use_mask=self.use_mask,
            mask=self.mask,
            mask_type=self.mask_type,
            return_cleaned_xyz=False,
            do_checks=False,
            verbosity=self.verbosity)

        dim, T = array.shape

        y = self._get_single_residuals(
            array, target_var=1, return_means=False)
        # Get RSS
        rss = (y**2).sum()
        # Number of parameters
        p = dim - 1
        # Get AIC
        score = T * numpy.log(rss) + 2. * p

        return score


class GPACE(CondIndTest):
    r"""GPACE conditional independence test.

    GPACE is based on a Gaussian process (GP) regression and a maximal
    correlation test on the residuals. GP is estimated with scikit-learn and
    allows to flexibly specify kernels and hyperparameters or let them be
    optimized automatically. The maximal correlation test is implemented with
    the ACE estimator either from a pure python implementation (slow) or, if rpy
    is available, using the R-package 'acepack'. Here the null distribution is
    not analytically available, but can be precomputed with the script
    'generate_gpace_nulldist.py' which generates a \*.npz file containing the
    null distribution for different sample sizes.

    Notes
    -----
    As described in [1]_, GPACE is based on a Gaussian
    process (GP) regression and a maximal correlation test on the residuals. To
    test :math:`X \perp Y | Z`, first  :math:`Z` is regressed out from :math:`X`
    and :math:`Y` assuming the  model

    .. math::  X & =  f_X(Z) + \epsilon_{X} \\
        Y & =  f_Y(Z) + \epsilon_{Y}  \\
        \epsilon_{X,Y} &\sim \mathcal{N}(0, \sigma^2)

    using GP regression. Here :math:`\sigma^2` corresponds to the gp_alpha
    parameter. Then the residuals  are transformed to uniform
    marginals yielding :math:`r_X,r_Y` and their dependency is tested with

    .. math::  \max_{g,h}\rho\left(g(r_X),h(r_Y)\right)

    where :math:`g,h` yielding maximal correlation are obtained using the
    Alternating Conditional Expectation (ACE) algorithm. The null distribution
    of the maximal correlation can be pre-computed.

    Parameters
    ----------
    null_dist_filename : str, otional (default: None)
        Path to file containing null distribution. If None is passed, the
        default filename generated by the script "generate_gpace_nulldist.py"
        is used.

    gp_version : {'new', 'old'}, optional (default: 'new')
        The older GP version from scikit-learn 0.17 was used for the numerical
        simulations in [1]_. The newer version from scikit-learn 0.19 is faster
        and allows more flexibility regarding kernels etc.

    gp_kernel : kernel object, optional (default: None)
        Only available for gp_version='new'. Can be any scikit-learn kernel
        object available in gaussian_process.kernels. The kernel specifies the
        covariance function of the GP. If None is passed, the kernel '1.0 *
        RBF(1.0)' is used as default. Note that the kernel's hyperparameters are
        optimized during fitting.

    gp_alpha :  float or array-like, optional (default: None)
        Only available for gp_version='new'. Value added to the diagonal of the
        kernel matrix during fitting. Larger values correspond to increased
        noise level in the observations and reduce potential numerical issues
        during fitting. If an array is passed, it must have the same number of
        entries as the data used for fitting and is used as datapoint-dependent
        noise level. Note that this is equivalent to adding a WhiteKernel with
        c=alpha. If None is passed, gp_alpha=1 is used.
    
    gp_restarts : int, optional (default: None)
        Only available for gp_version='new'. The number of restarts of the
        optimizer for finding the kernel's parameters which maximize the log-
        marginal likelihood. The first run of the optimizer is performed from
        the kernel's initial parameters, the remaining ones (if any) from thetas
        sampled log-uniform randomly from the space of allowed theta-values. If
        greater than 0, all bounds must be finite. If None is passed,
        n_restarts_optimizer=0 is used, implying that one run is performed.

    ace_version : {'python', 'acepack'}
        Estimator for ACE estimator of maximal correlation to use. 'python'
        loads the very slow pure python version available from
        https://pypi.python.org/pypi/ace/0.3. 'acepack' loads the much faster
        version from the R-package acepack. This requires the R-interface
        rpy2 to be installed and acepack needs to be installed in R beforehand.
        Note that both versions 'python' and 'acepack' may result in different
        results. In [1]_ the acepack version was used.

    **kwargs : 
        Arguments passed on to parent class CondIndTest.

    """
    def __init__(self,
                null_dist_filename=None,
                gp_version='new',
                gp_kernel=None,
                gp_alpha=None,
                gp_restarts=None,
                ace_version='acepack',
                **kwargs):

        CondIndTest.__init__(self, **kwargs)

        self.gp_version = gp_version
        self.ace_version = ace_version

        self.gp_kernel = gp_kernel
        self.gp_alpha = gp_alpha
        self.gp_restarts = gp_restarts

        self.measure = 'gp_ace'
        self.two_sided = False
        self.residual_based = True

        # Load null-dist file, adapt if necessary
        if null_dist_filename is None:
            if self.ace_version == 'python':
                null_dist_filename = '../gpace_nulldists_purepython.npz'
            elif self.ace_version == 'acepack':
                null_dist_filename = '../gpace_nulldists_acepack.npz'
        null_dist_file = numpy.load(null_dist_filename)
        self.sample_sizes = null_dist_file['T']
        self.null_dist = null_dist_file['exact_dist']
        self.null_samples = len(self.null_dist[0])

    def _remove_ties(self, array, verbosity=0):
        """Removes ties from array by adding noise.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns.
        
        Returns
        -------
        array : array-like
            Array with noise added.
        """
        array = array + 1E-10 * numpy.random.rand(*array.shape)
        # assert len(numpy.unique(array)) == numpy.size(array)
        return array
    
    # @profile
    def _get_single_residuals(self, array, target_var,
                              return_means=False, 
                              standardize=True,
                              return_likelihood=False):
        """Returns residuals of Gaussian process regression.

        Performs a GP regression of the variable indexed by target_var on the
        conditions Z. Here array is assumed to contain X and Y as the first two
        rows with the remaining rows (if present) containing the conditions Z.
        Optionally returns the estimated mean and the likelihood.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        target_var : {0, 1}
            Variable to regress out conditions from.

        standardize : bool, optional (default: True)
            Whether to standardize the array beforehand.

        return_means : bool, optional (default: False)
            Whether to return the estimated regression line.

        return_likelihood : bool, optional (default: False)
            Whether to return the log_marginal_likelihood of the fitted GP

        Returns
        -------
        resid [, mean, likelihood] : array-like
            The residual of the regression and optionally the estimated mean
            and/or the likelihood.
        """

        dim, T = array.shape

        if dim <= 2:
            if return_likelihood:
                return array[target_var, :], -numpy.inf
            else:
                return array[target_var, :]

        # Standardize
        if standardize:
            array -= array.mean(axis=1).reshape(dim, 1)
            array /= array.std(axis=1).reshape(dim, 1)
            if numpy.isnan(array).sum() != 0:
                raise ValueError("nans after standardizing, "
                                 "possibly constant array!")

        var = array[target_var, :]
        z = numpy.fastCopyAndTranspose(array[2:])
        if numpy.ndim(z) == 1:
            z = z.reshape(-1, 1)

        if self.gp_version == 'old':
            # Old GP failed for ties in the data
            z = self._remove_ties(z)

            gp = gaussian_process.GaussianProcess(
                nugget=1E-1,
                thetaL=1E-16,
                thetaU=numpy.inf,
                corr='squared_exponential',
                optimizer='fmin_cobyla',
                regr='constant',
                normalize=False,
                storage_mode='light')

        elif self.gp_version == 'new':
            if self.gp_kernel is None:
                self.gp_kernel = gaussian_process.kernels.RBF(
                    length_scale=1.0,
                    # length_scale_bounds=(1E-16, numpy.inf)  #(1e-05, 100000.0)
                    )
            if self.gp_alpha is None:
                self.gp_alpha = 0.1
            if self.gp_restarts is None:
                self.gp_restarts = 0

            gp = gaussian_process.GaussianProcessRegressor(
                kernel=self.gp_kernel,
                alpha=self.gp_alpha,
                optimizer='fmin_l_bfgs_b',  
                n_restarts_optimizer=self.gp_restarts,
                normalize_y=False,
                copy_X_train=True,
                random_state=None)

        gp.fit(z, var.reshape(-1, 1))

        if return_likelihood:
            likelihood = gp.log_marginal_likelihood()

        mean = gp.predict(z).squeeze()

        resid = var - mean

        if return_means and return_likelihood==False:
            return (resid, mean)
        elif return_means==False and return_likelihood:
            return (resid, likelihood)
        elif return_means and return_likelihood:
            return resid, mean, likelihood
        else:
            return resid

    def get_dependence_measure(self, array, xyz):
        """Return GPACE measure.

        Estimated as the maximal correlation of the residuals of a GP
        regression.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).

        Returns
        -------
        val : float
            GPACE test statistic.    
        """

        D, T = array.shape

        x = self._get_single_residuals(array, target_var=0)
        y = self._get_single_residuals(array, target_var=1)

        val = self._get_maxcorr(numpy.array([x, y]))

        return val

    def _trafo2uniform(self, x):
        """Transforms input array to uniform marginals. 

        Assumes x.shape = (dim, T)

        Parameters
        ----------
        x : array-like
            Input array.

        Returns
        -------
        u : array-like
            array with uniform marginals.
        """

        def trafo(xi):
            xisorted = numpy.sort(xi)
            yi = numpy.linspace(1. / len(xi), 1, len(xi))
            return numpy.interp(xi, xisorted, yi)

        if numpy.ndim(x) == 1:
            u = trafo(x)
        else:
            u = numpy.empty(x.shape)
            for i in range(x.shape[0]):
                u[i] = trafo(x[i])
        return u

    # @profile
    def _get_maxcorr(self, array_resid):
        """Return maximal correlation coefficient estimated by ACE.

        Method is described in [1]_. The maximal correlation test is implemented
        with the ACE estimator either from a pure python implementation
        (slow) or, if rpy is available, using the R-package 'acepack'. The
        variables are transformed to uniform marginals using the empirical
        cumulative distribution function beforehand. Here the null
        distribution is not analytically available, but can be precomputed
        with the script 'generate_gpace_nulldist.py' which generates a \*.npz
        file containing the null distribution. 

        Parameters 
        ---------- 
        array_resid : array-like     
            data array must be of shape (2, T)

        Returns
        -------
        val : float
            Maximal correlation coefficient.
        """

        # Remove ties before applying transformation to uniform marginals
        # array_resid = self._remove_ties(array_resid, verbosity=4)

        x, y = self._trafo2uniform(array_resid)

        if self.ace_version == 'python':
            class Suppressor(object):
                """Wrapper class to prevent output from ACESolver."""
                def __enter__(self):
                    self.stdout = sys.stdout
                    sys.stdout = self
                def __exit__(self, type, value, traceback):
                    sys.stdout = self.stdout
                def write(self, x): 
                    pass
            myace = ace.ace.ACESolver()
            myace.specify_data_set([x], y)
            with Suppressor():
                myace.solve()
            val = numpy.corrcoef(myace.x_transforms[0], myace.y_transform)[0,1]
        
        elif self.ace_version == 'acepack':
            ace_rpy = rpy2.robjects.r['ace'](x, y)
            val = numpy.corrcoef(numpy.asarray(ace_rpy[8]).flatten(), 
                                 numpy.asarray(ace_rpy[9]))[0, 1]
        else:
            raise ValueError("ace_version must be 'python' or 'acepack'")
        
        return val


    def get_analytic_significance(self, value, df):
        """Returns p-value for the maximal correlation coefficient.
        
        The null distribution is loaded and the entry for the nearest available
        degrees of freedom (df) is used. If it is different by more than 1% from
        the actual sample size, an error is raised. Then the null distribution
        has to be generated with the script "generate_gpace_nulldist.py". The
        maximal correlation coefficient is one-sided. If the degrees of freedom
        are less than 1, numpy.nan is returned.
        
        Parameters
        ----------
        value : float
            Test statistic value.

        df : int
            degrees of freedom of the test, given by T - dim

        Returns
        -------
        pval : float or numpy.nan
            P-value.
        """

        if df < 1:
            pval = numpy.nan
        else:
            idx_near = (numpy.abs(self.sample_sizes - df)).argmin()

            if numpy.abs(self.sample_sizes[idx_near] - df) / float(df) > 0.01:
                raise ValueError("Null distribution for GPACE not available "
                             "for deg. of freed. = %d, nearest values "
                             "= %s." % (int(df), 
                             self.sample_sizes[max(0,idx_near-1):idx_near+2])+
                             " Use script to generate nulldist."
                             "" )
            null_dist_here = self.null_dist[idx_near]
            pval = numpy.mean(null_dist_here > numpy.abs(value))

        return pval

    def get_analytic_confidence(self, value, df, conf_lev):
        """Placeholder function, not available."""
        raise ValueError("Analytic confidence not implemented for %s"
                         "" % self.measure)

    def get_model_selection_criterion(self, j,
                                      parents,
                                      tau_max=0):
        """Returns log marginal likelihood for GP regression.
        
        Fits a GP model of the parents to variable j and returns the negative
        log marginal likelihood as a model selection score. Is used to determine
        optimal hyperparameters in PCMCI, in particular the pc_alpha value.
        
        Parameters
        ----------
        j : int
            Index of target variable in data array.

        parents : list
            List of form [(0, -1), (3, -2), ...] containing parents.

        tau_max : int, optional (default: 0)
            Maximum time lag. This may be used to make sure that estimates for
            different lags in X, Z, all have the same sample size.
        
        Returns:
        score : float
            Model score.
        """

        Y = [(j, 0)]
        X = [(j, 0)]   # dummy variable here
        Z = parents
        array, xyz = self._construct_array(
            X=X, Y=Y, Z=Z,
            tau_max=tau_max,
            data=self.data,
            use_mask=self.use_mask,
            mask=self.mask,
            mask_type=self.mask_type,
            return_cleaned_xyz=False,
            do_checks=False,
            verbosity=self.verbosity)

        dim, T = array.shape

        y, logli = self._get_single_residuals(array,
                            target_var=1, return_likelihood=True)

        score = -logli

        return score


class CMIknn(CondIndTest):
    r"""Conditional mutual information test based on nearest-neighbor estimator.

    Conditional mutual information is the most general dependency measure
    coming from an information-theoretic framework. It makes no assumptions
    about the parametric form of the dependencies by directly estimating the
    underlying joint density. The test here is based on the estimator in  S.
    Frenzel and B. Pompe, Phys. Rev. Lett. 99, 204101 (2007), combined with a
    shuffle test to generate  the distribution under the null hypothesis of
    independence. The knn-estimator is suitable only for variables taking a 
    continuous range of values. For discrete variables use the CMIsymb class.

    Notes
    -----
    CMI is given by

    .. math:: I(X;Y|Z) &= \int p(z)  \iint  p(x,y|z) \log 
                \frac{ p(x,y |z)}{p(x|z)\cdot p(y |z)} \,dx dy dz

    Its knn-estimator is given by 

    .. math:: \widehat{I}(X;Y|Z)  &=   \psi (k) + \frac{1}{T} \sum_{t=1}^T 
            \left[ \psi(k_{Z,t}) - \psi(k_{XZ,t}) - \psi(k_{YZ,t}) \right]

    where :math:`\psi` is the Digamma function.  This estimator has as a
    parameter the number of nearest-neighbors :math:`k` which determines the
    size of hyper-cubes around each (high-dimensional) sample point. Then
    :math:`k_{Z,},k_{XZ},k_{YZ}` are the numbers of neighbors in the respective
    subspaces.

    :math:`k` can be viewed as a density smoothing parameter (although it is
    data-adaptive unlike fixed-bandwidth estimators). For large :math:`k`, the
    underlying dependencies are more smoothed and CMI has a larger bias,
    but lower variance, which is more important for significance testing. Note
    that the estimated CMI values can be slightly negative while CMI is a non-
    negative quantity.

    This class requires the scipy.spatial.cKDTree package and the tigramite
    cython module.

    Parameters
    ----------
    knn : int, optional (default: 100)
        Number of nearest-neighbors which determines the size of hyper-cubes
        around each (high-dimensional) sample point.

    **kwargs : 
        Arguments passed on to parent class CondIndTest.
    """
    def __init__(self,
                knn=100,
                **kwargs):

        CondIndTest.__init__(self, **kwargs)

        self.knn = knn

        self.measure = 'cmi_knn'
        self.two_sided = False
        self.residual_based = False
        self.recycle_residuals = False

    def _trafo2uniform(self, x):
        """Transforms input array to uniform marginals. 

        Assumes x.shape = (dim, T)

        Parameters
        ----------
        x : array-like
            Input array.

        Returns
        -------
        u : array-like
            array with uniform marginals.
        """

        def trafo(xi):
            xisorted = numpy.sort(xi)
            yi = numpy.linspace(1. / len(xi), 1, len(xi))
            return numpy.interp(xi, xisorted, yi)

        if numpy.ndim(x) == 1:
            u = trafo(x)
        else:
            u = numpy.empty(x.shape)
            for i in range(x.shape[0]):
                u[i] = trafo(x[i])
        return u

    def _get_nearest_neighbors(self, array, xyz, knn, transform='standardize'):
        """Returns nearest neighbors according to Frenzel and Pompe (2007).

        Retrieves the distances eps to the k-th nearest neighbors for every
        sample in joint space XYZ and returns the numbers of nearest neighbors
        within eps in subspaces Z, XZ, YZ.

        Parameters 
        ---------- 
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).
       
        knn : int
            Number of nearest-neighbors which determines the size of hyper-cubes
            around each (high-dimensional) sample point.

        transform : {'standardize', 'uniform', False}, optional 
            (default: 'standardize')
            Whether to transform the array beforehand by standardizing
            or transforming to uniform marginals.

        Returns
        -------
        k_xz, k_yz, k_z : tuple of arrays of shape (T,)
            Nearest neighbors in subspaces.
        """

        dim, T = array.shape

        if transform == 'standardize':
            # Standardize
            array = array.astype('float')
            array -= array.mean(axis=1).reshape(dim, 1)
            array /= array.std(axis=1).reshape(dim, 1)
            # FIXME: If the time series is constant, return nan rather than
            # raising Exception
            if numpy.isnan(array).sum() != 0:
                raise ValueError("nans after standardizing, "
                                 "possibly constant array!")
        elif transform == 'uniform':
            array = self._trafo2uniform(array)

        # Add noise to destroy ties...
        array += (1E-6 * array.std(axis=1).reshape(dim, 1)
                  * numpy.random.rand(array.shape[0], array.shape[1]))

        # Use cKDTree to get distances eps to the k-th nearest neighbors for
        # every sample in joint space XYZ with maximum norm
        tree_xyz = spatial.cKDTree(array.T)
        epsarray = tree_xyz.query(array.T, k=knn+1, p=numpy.inf,
                                  eps=0.)[0][:,knn].astype('float')

        # Prepare for fast cython access
        dim_x = int(numpy.where(xyz == 0)[0][-1] + 1)
        dim_y = int(numpy.where(xyz == 1)[0][-1] + 1 - dim_x)

        k_xz, k_yz, k_z = \
         tigramite_cython_code._get_neighbors_within_eps_cython(array, T, dim_x,
         dim_y, epsarray, knn, dim)

        return k_xz, k_yz, k_z

    def get_dependence_measure(self, array, xyz):
        """Returns CMI estimate as described in Frenzel and Pompe PRL (2007).

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).
        
        Returns
        -------
        val : float
            Conditional mutual information estimate.
        """

        k_xz, k_yz, k_z = self._get_nearest_neighbors(array=array, xyz=xyz,
                                                 knn=self.knn)

        val = special.digamma(self.knn) - (special.digamma(k_xz) +
                                      special.digamma(k_yz) -
                                      special.digamma(k_z)).mean()

        return val

    def get_analytic_significance(self, value, df):
        """Placeholder function, not available."""
        raise ValueError("Analytic confidence not implemented for %s"
                         "" % self.measure)

    def get_analytic_confidence(self, value, df, conf_lev):
        """Placeholder function, not available."""
        raise ValueError("Analytic confidence not implemented for %s"
                         "" % self.measure)

    def get_model_selection_criterion(self, j,
                                      parents,
                                      tau_max=0):
        """Placeholder function, not available."""
        raise ValueError("Model selection not implemented for %s"
                         "" % self.measure)


class CMIsymb(CondIndTest):
    r"""Conditional mutual information test based on discrete estimator.

    Conditional mutual information is the most general dependency measure
    coming from an information-theoretic framework. It makes no assumptions
    about the parametric form of the dependencies by directly estimating the
    underlying joint density. The test here is based on directly estimating
    the joint distribution assuming symbolic input, combined with a
    shuffle test to generate  the distribution under the null hypothesis of
    independence. The knn-estimator is suitable only for discrete variables.
    For continuous variables, either pre-process the data using the functions
    in data_processing or use the CMIknn class.

    Notes
    -----
    CMI and its estimator are given by

    .. math:: I(X;Y|Z) &= \sum p(z)  \sum \sum  p(x,y|z) \log 
                \frac{ p(x,y |z)}{p(x|z)\cdot p(y |z)} \,dx dy dz

    Parameters
    ----------
    **kwargs : 
        Arguments passed on to parent class CondIndTest.
    """
    def __init__(self,
                **kwargs):

        CondIndTest.__init__(self, **kwargs)

        self.measure = 'cmi_symb'
        self.two_sided = False
        self.residual_based = False
        self.recycle_residuals = False

        if self.conf_blocklength is None or self.sig_blocklength is None:
            warnings.warn("Automatic block-length estimations from decay of "
                          "autocorrelation may not be sensical for discrete "
                          "data")

    def _bincount_hist(self, symb_array, weights=None):
        """Computes histogram from symbolic array.

        The maximum of the symbolic array determines the alphabet / number
        of bins.

        Parameters
        ----------
        symb_array : integer array
            Data array of shape (dim, T).
            
        weights : float array, optional (default: None)
            Optional weights array of shape (dim, T).

        Returns
        -------
        hist : array
            Histogram array of shape (base, base, base, ...)*number of
            dimensions with Z-dimensions coming first.
        """

        bins = int(symb_array.max() + 1)

        dim, T = symb_array.shape

        # Needed because numpy.bincount cannot process longs
        if type(bins ** dim) != int:
            raise ValueError("Too many bins and/or dimensions, "
                             "numpy.bincount cannot process longs")
        if bins ** dim * 16. / 8. / 1024. ** 3 > 3.:
            raise ValueError("Dimension exceeds 3 GB of necessary "
                             "memory (change this code line if more...)")
        if dim * bins ** dim > 2 ** 65:
            raise ValueError("base = %d, D = %d: Histogram failed: "
                             "dimension D*base**D exceeds int64 data type"
                             % (bins, dim))

        flathist = numpy.zeros((bins ** dim), dtype='int16')
        multisymb = numpy.zeros(T, dtype='int64')
        if weights is not None:
            flathist = numpy.zeros((bins ** dim), dtype='float32')
            multiweights = numpy.ones(T, dtype='float32')

        # print numpy.prod(weights, axis=0)
        for i in range(dim):
            multisymb += symb_array[i, :] * bins ** i
            if weights is not None:
                multiweights *= weights[i, :]
                # print i, multiweights

        if weights is None:
            result = numpy.bincount(multisymb)
            # print result
        else:
            result = (numpy.bincount(multisymb, weights=multiweights)
                      / multiweights.sum())

        flathist[:len(result)] += result

        hist = flathist.reshape(tuple([bins, bins] +
                                      [bins for i in range(dim - 2)])).T

        return hist

    def get_dependence_measure(self, array, xyz):
        """Returns CMI estimate based on bincount histogram.

        Parameters
        ----------
        array : array-like
            data array with X, Y, Z in rows and observations in columns

        xyz : array of ints
            XYZ identifier array of shape (dim,).
        
        Returns
        -------
        val : float
            Conditional mutual information estimate.
        """

        dim, T = array.shape

        # High-dimensional Histogram
        hist = self._bincount_hist(array, weights=None)

        def _plogp_vector(T):
            """Precalculation of p*log(p) needed for entropies."""
            gfunc = numpy.zeros(T + 1, dtype='float')
            gfunc = numpy.zeros(T + 1)
            gfunc[1:] = numpy.arange(
                1, T + 1, 1) * numpy.log(numpy.arange(1, T + 1, 1))
            def plogp_func(t):
                return gfunc[t]
            return numpy.vectorize(plogp_func)
        
        plogp = _plogp_vector(T)
        
        hxyz = (-(plogp(hist)).sum() + plogp(T)) / float(T)
        hxz = (-(plogp(hist.sum(axis=1))).sum() + plogp(T)) / \
            float(T)
        hyz = (-(plogp(hist.sum(axis=0))).sum() + plogp(T)) / \
            float(T)
        hz = (-(plogp(hist.sum(axis=0).sum(axis=0))).sum() +
              plogp(T)) / float(T)

        # else:
        #     def plogp_func(p):
        #         if p == 0.: return 0.
        #         else: return p*numpy.log(p)
        #     plogp = numpy.vectorize(plogp_func)

        #     hxyz = -plogp(hist).sum()
        #     hxz = -plogp(hist.sum(axis=1)).sum()
        #     hyz = -plogp(hist.sum(axis=0)).sum()
        #     hz = -plogp(hist.sum(axis=0).sum(axis=0)).sum()

        val = hxz + hyz - hz - hxyz

        return val

    def get_analytic_significance(self, value, df):
        """Placeholder function, not available."""
        raise ValueError("Analytic confidence not implemented for %s"
                         "" % self.measure)

    def get_analytic_confidence(self, value, df, conf_lev):
        """Placeholder function, not available."""
        raise ValueError("Analytic confidence not implemented for %s"
                         "" % self.measure)

    def get_model_selection_criterion(self, j,
                                      parents,
                                      tau_max=0):
        """Placeholder function, not available."""
        raise ValueError("Model selection not implemented for %s"
                         "" % self.measure)

if __name__ == '__main__':

    # Quick test
    import data_processing as pp
    # numpy.random.seed(44)
    a = 0.
    c = 0.6
    T = 1000
    # Each key refers to a variable and the incoming links are supplied as a
    # list of format [((driver, lag), coeff), ...]
    links_coeffs = {0: [((0, -1), a)],
                    1: [((1, -1), a), ((0, -1), c)],
                    2: [((2, -1), a), ((1, -1), c)]   #, ((0, -2), c)],
                    }

    data, true_parents_neighbors = pp.var_process(links_coeffs,
                                                  use='inv_inno_cov', T=T)

    data_mask = numpy.zeros(data.shape)

    # cond_ind_test = ParCorr(
    #     significance='analytic',
    #     sig_samples=100,

    #     confidence='bootstrap', #'bootstrap',
    #     conf_lev=0.9,
    #     conf_samples=100,
    #     conf_blocklength=1,

    #     use_mask=False,
    #     mask_type=['y'],
    #     recycle_residuals=False,
    #     verbosity=3)

    # cond_ind_test = GPACE(
    #     significance='shuffle_test',
    #     sig_samples=100,

    #     confidence=False, # False  'bootstrap',
    #     conf_lev=0.9,
    #     conf_samples=100,
    #     conf_blocklength=None,

    #     use_mask=False,
    #     mask_type=['y'],

    #     null_dist_filename=None,
    #     gp_version='new',
    #     gp_kernel=None,
    #     gp_alpha=None,
    #     gp_restarts=None,
    #     ace_version='acepack',
    #     recycle_residuals=False,
    #     verbosity=4)


    # cond_ind_test = CMIknn(
    #     significance='shuffle_test',
    #     sig_samples=1000,
    #     knn=100,
    #     confidence='bootstrap', #'bootstrap',
    #     conf_lev=0.9,
    #     conf_samples=100,
    #     conf_blocklength=None,

    #     use_mask=False,
    #     mask_type=['y'],
    #     recycle_residuals=False,
    #     verbosity=3)

    cond_ind_test = CMIsymb(
        significance='shuffle_test',
        sig_samples=1000,

        confidence='bootstrap', #'bootstrap',
        conf_lev=0.9,
        conf_samples=100,
        conf_blocklength=None,

        use_mask=False,
        mask_type=['y'],
        recycle_residuals=False,
        verbosity=3)

    if cond_ind_test.measure == 'cmi_symb':
        data = pp.quantile_bin_array(data, bins=6)

    dataframe = pp.DataFrame(data)
    cond_ind_test.set_dataframe(dataframe)

    X = [(0, -2)]
    Y = [(2, 0)]
    Z = [(1, -1)]  #(2, -1), (1, -1), (0, -3)]  #[(1, -1)]  #[(2, -1), (1, -1), (0, -3)] # [(2, -1), (1, -1), (2, -3)]   [(1, -1)]
    val, pval = cond_ind_test.run_test(X, Y, Z, tau_max=5)
    conf_interval = cond_ind_test.get_confidence(X, Y, Z, tau_max=5)

    print ("I(X,Y|Z) = %.2f [%.2f, %.2f] | p-value = %.3f " % 
                      (val, conf_interval[0], conf_interval[1], pval))