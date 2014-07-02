'''
Functions and classes for infilling missing observations in an
incomplete station time series using probabilistic principal component analysis (PPCA):

Stacklies W, Redestig H, Scholz M, Walther D, Selbig J. 2007. 
pcaMethods-a bioconductor package providing PCA methods for incomplete data.
Bioinformatics 23: 1164-1167. DOI: 10.1093/bioinformatics/btm069.
'''

import numpy as np
from twx.db import STN_ID, LON, LAT, UTC_OFFSET
from twx.utils import pca_svd, grt_circle_dist
import os
from scipy import stats
#rpy2
import rpy2
import rpy2.robjects as robjects
from rpy2.robjects.numpy2ri import numpy2ri
robjects.conversion.py2ri = numpy2ri
r = robjects.r


MIN_POR_OVERLAP = 2.0 / 3.0
MAX_DISTANCE = 100
MAX_NNR_VAR = 0.99
MIN_NNR_VAR = 0.90
MIN_DAILY_NGHBRS = 3
NNGH_NNR = 4
R_LOADED = False


class InfillMatrixPPCA(object):
    '''
    A class for building a data matrix of surrounding neighbor station observations for a 
    target station to run PPCA missing value infilling.
    '''
    
    def __init__(self, stn_id, stn_da, tair_var, nnr_ds, min_dist= -1, max_dist=MAX_DISTANCE, tair_mask=None,add_bestngh=True):
        '''
        Parameters
        ----------
        stn_id : str
            The station id of the target station
        stn_da : twx.db.StationDataDb
            The station database from which all target and neighboring
            station observations should be loaded
        tair_var : str
            The temperature variable ('tmin' or 'tmax') of focus.
        nnr_ds : twx.db.NNRNghData
            A NNRNghData object for loading reanalysis data to help supplement
            the neighboring station data.
        min_dist : int, optional
            The minimum distance (exclusive) for which to search for neighboring stations.
            Pass -1 if there should be no minimum distance
        max_dist : int, optional
            The maximum distance (inclusive) for which to search for neighboring stations.
            Defaults to MAX_DISTANCE
        tair_mask : ndarray, optional
            A boolean mask specifying which observations at the target should
            artificially be set to nan. This can be used for cross-validation.
            Mask size must equal the time series length specified by the passed
            StationDataDb.
        add_bestngh : boolean optional
            Add the best correlated neighbor to the data matrix even if the time
            series period-of-record of the neighbor is less than the
            MIN_POR_OVERLAP threshold for the entire period over which 
            the target station is being infilled.
        '''
        
        _load_R()
        
        idx_target = np.nonzero(stn_da.stn_ids == stn_id)[0][0]
        
        stn = stn_da.stns[idx_target]
        
        target_tair = stn_da.load_all_stn_obs_var(np.array([stn_id]), tair_var)[0]
        target_norm = stn_da.get_stn_mean(tair_var,idx_target)
        target_std = stn_da.get_stn_std(tair_var,idx_target)
        
        if tair_mask is not None:
            target_tair[tair_mask] = np.nan
                    
        #Number of observations threshold for entire period that is being infilled
        nthres_all = np.round(MIN_POR_OVERLAP * target_tair.size)
        
        #Number f observations threshold just for the target's period of record
        valid_tair_mask = np.isfinite(target_tair)
        ntair_valid = np.nonzero(valid_tair_mask)[0].size
        nthres_target_por = np.round(MIN_POR_OVERLAP * ntair_valid)    
        
        #Make sure to not include the target station itself as a neighbor station
        stns_mask = np.logical_and(np.logical_and(stn_da.stns[STN_ID] != stn_id,
                                                  np.isfinite(stn_da.get_stn_mean(tair_var))),
                                   stn_da.stns[STN_ID] != 'GHCN_CA002503650')
        all_stns = stn_da.stns[stns_mask]
        
        dists = grt_circle_dist(stn[LON], stn[LAT], all_stns[LON], all_stns[LAT])
        mask_dists = np.logical_and(dists <= max_dist, dists > min_dist)
        
        while np.nonzero(mask_dists)[0].size == 0:
            max_dist += MAX_DISTANCE / 2.0
            mask_dists = np.logical_and(dists <= max_dist, dists > min_dist)
        
        ngh_stns = all_stns[mask_dists]
        ngh_dists = dists[mask_dists]
        
        ngh_ids = ngh_stns[STN_ID]
        nghid_mask = np.in1d(stn_da.stn_ids, ngh_ids, assume_unique=True)
        ngh_norms = stn_da.get_stn_mean(tair_var,nghid_mask)
        ngh_std = stn_da.get_stn_std(tair_var,nghid_mask)
        ngh_tair = stn_da.load_all_stn_obs_var(ngh_ids, tair_var, set_flagged_nan=True)[0]
        
        if len(ngh_tair.shape) == 1:
            ngh_tair.shape = (ngh_tair.size, 1) 
        
        dist_sort = np.argsort(ngh_dists)
        ngh_stns = ngh_stns[dist_sort]
        ngh_dists = ngh_dists[dist_sort]
        ngh_norms = ngh_norms[dist_sort]
        ngh_std = ngh_std[dist_sort]
        ngh_tair = ngh_tair[:, dist_sort]
        
        overlap_mask_tair = np.zeros(ngh_stns.size, dtype=np.bool)
        ioa = np.zeros(ngh_stns.size)
        
        best_ioa = 0
        i = None
        
        for x in np.arange(ngh_stns.size):
            
            valid_ngh_mask = np.isfinite(ngh_tair[:, x])
            
            nlap = np.nonzero(valid_ngh_mask)[0].size
            
            overlap_mask = np.logical_and(valid_tair_mask, valid_ngh_mask)
            
            nlap_stn = np.nonzero(overlap_mask)[0].size
            
            if nlap >= nthres_all and nlap_stn >= nthres_target_por:
                
                ioa[x] = _calc_ioa(target_tair[overlap_mask], ngh_tair[:, x][overlap_mask])
                overlap_mask_tair[x] = True
            
            elif nlap_stn >= nthres_target_por and add_bestngh:
                
                aioa = _calc_ioa(target_tair[overlap_mask], ngh_tair[:, x][overlap_mask])
                
                if aioa > best_ioa:
                    
                    ioa[x] = aioa
                    overlap_mask_tair[x] = True
                    
                    if i != None:
                        overlap_mask_tair[i] = False
                    
                    i = x
                    best_ioa = aioa
        
        if add_bestngh and i is not None:
            
            if ioa[i] != np.max(ioa) or ioa[i] < 0.7:
                
                overlap_mask_tair[i] = False
                
        ioa = ioa[overlap_mask_tair]
        ngh_dists = ngh_dists[overlap_mask_tair]
        ngh_tair = ngh_tair[:, overlap_mask_tair]
        ngh_norms = ngh_norms[overlap_mask_tair]
        ngh_std = ngh_std[overlap_mask_tair]
        
        if ioa.size > 0:
            
            ioa_sort = np.argsort(ioa)[::-1]
            ioa = ioa[ioa_sort]
            ngh_dists = ngh_dists[ioa_sort]
            ngh_tair = ngh_tair[:, ioa_sort]
            ngh_norms = ngh_norms[ioa_sort]
            ngh_std = ngh_std[ioa_sort]
            
            target_tair.shape = (target_tair.size, 1)
            
            pca_tair = np.hstack((target_tair, ngh_tair))
            ngh_dists = np.concatenate((np.zeros(1), ngh_dists))
            ioa = np.concatenate((np.ones(1), ioa))
            ngh_norms = np.concatenate((np.array([target_norm]), ngh_norms))
            ngh_std = np.concatenate((np.array([target_std]), ngh_std))
            
            valid_pca_mask = np.isfinite(pca_tair)
            
            nnghs_per_day = np.sum(valid_pca_mask , axis=1)
        
        else:
            
            target_tair.shape = (target_tair.size, 1)  
            pca_tair = target_tair
            
            valid_tair_mask.shape = (valid_tair_mask.size, 1) 
            valid_pca_mask = valid_tair_mask
            
            ioa = np.ones(1)
            ngh_dists = np.zeros(1)
            ngh_norms = np.array([target_norm])
            ngh_std = np.array([target_std])
            
            nnghs_per_day = np.zeros(target_tair.shape[0])        
        
        #############################################################
        self.pca_tair = np.array(pca_tair, dtype=np.float64)
        self.valid_pca_mask = valid_pca_mask
        self.ngh_ioa = ioa
        self.ngh_dists = ngh_dists
        self.ngh_norms = ngh_norms
        self.ngh_std = ngh_std
        self.max_dist = max_dist
        self.stn_id = stn_id
        self.stn_da = stn_da
        self.tair_var = tair_var
        self.tair_mask = tair_mask
        self.nnghs_per_day = nnghs_per_day
        self.nnr_ds = nnr_ds
        self.stn = stn
    
    def __extend_ngh_radius(self, extend_by):
        '''
        Extend the search radius for neighboring stations.
        The minimum of the search radius is the previous max distance.
        
        Parameters
        ----------
        extend_by: int 
            The amount (km) by which to extend the radius.
        '''
        
        min_dist = self.max_dist
        max_dist = self.max_dist + extend_by

        pca_matrix2 = InfillMatrixPPCA(self.stn_id, self.stn_da, self.tair_var,self.nnr_ds, min_dist, 
                                       max_dist, self.tair_mask,add_bestngh=False)

        self.__merge(pca_matrix2)
        self.max_dist = max_dist

    def __merge(self, matrix2):
        '''
        Merge this InfillMatrixPPCA with another InfillMatrixPPCA
        
        Parameters
        ----------
        matrix2: InfillMatrixPPCA
            The other InfillMatrixPPCA with which to merge
        '''
    
        self.pca_tair = np.hstack((self.pca_tair, matrix2.pca_tair[:, 1:]))
        self.valid_pca_mask = np.hstack((self.valid_pca_mask, matrix2.valid_pca_mask[:, 1:]))
        self.ngh_ioa = np.concatenate((self.ngh_ioa, matrix2.ngh_ioa[1:]))
        self.ngh_dists = np.concatenate((self.ngh_dists, matrix2.ngh_dists[1:]))
        self.ngh_norms = np.concatenate((self.ngh_norms, matrix2.ngh_norms[1:]))
        self.ngh_std = np.concatenate((self.ngh_std, matrix2.ngh_std[1:]))
        
        if self.ngh_ioa.size > 0:
            
            ioa_sort = np.argsort(self.ngh_ioa[1:])[::-1]
            ioa_sort = np.concatenate([np.zeros(1, dtype=np.int), ioa_sort + 1])
            
            self.pca_tair = self.pca_tair[:, ioa_sort]
            self.valid_pca_mask = self.valid_pca_mask[:, ioa_sort]
            self.ngh_ioa = self.ngh_ioa[ioa_sort]
            self.ngh_dists = self.ngh_dists[ioa_sort]
            self.ngh_norms = self.ngh_norms[ioa_sort]
            self.ngh_std = self.ngh_std[ioa_sort]
            
            self.nnghs_per_day = np.sum(self.valid_pca_mask[:, 1:], axis=1)
    
        else:
            
            self.nnghs_per_day = np.zeros(self.pca_tair.shape[1])

    def __has_min_daily_nghs(self, nnghs, min_daily_nghs):
        '''
        Check to see if there is a minimum number of 
        neighbor observations each day
        '''
        
        trim_valid_mask = self.valid_pca_mask[:, 0:1 + nnghs]
        nnghs_per_day = np.sum(trim_valid_mask[:, 1:], axis=1)
        
        return np.min(nnghs_per_day) >= min_daily_nghs

    def infill(self, min_daily_nnghs=MIN_DAILY_NGHBRS,nnghs_nnr=NNGH_NNR,max_nnr_var=MAX_NNR_VAR,chk_perf=True,npcs=0,frac_obs_initnpcs=0.5,ppca_varyexplain=0.99,ppcaConThres=1e-5,verbose=False):
        '''
        Infill missing values for an incomplete station time series.
        
        Parameters
        ----------
        min_daily_nnghs : int, optional
            The minimum neighbors required for each day.
        nnghs_nnr : int, optional
            The number of neighboring NCEP/NCAR Reanalysis grid cells
        max_nnr_var : float, optional
            The required variance explained by principal components of
            a S-Mode PCA of the reanalysis data.
        chk_perf : boolean, optional
            If true, check performance of infilled output and if there
            are any bad infilled values. If there are bad infilled values,
            PPCA will be rerun with different configurations to try to eliminate
            the bad values.
        npcs : int, optional
            Use a specific set number of PCs. If npcs = 0, the number of PCs is determined
            dynamically based on ppca_varyexplain.
        frac_obs_initnpcs : float, optional
            The fraction of the total number of columns that should be used as the
            initial number of PCs. Example: if frac_obs is 0.5 and the number of columns
            is 10, the initial number of PCs will be 5.
        ppca_varyexplain : float, optional
            The required variance to be explained by the PCs. Example: if 0.99, the first
            n PCs that account for 99% of the variance in pca_matrix will be used
        ppcaConThres : float, optional
            The convergence threshold for the PPCA algorithm.
        verbose : boolean, optional
            If true, output PPCA algorithm progress.
        
        Returns
        ----------
        fnl_tair : ndarray
            Time series of station observations with missing values infilled
        mask_infill : ndarray
            Boolean array specifying which values were infilled in fnl_tair
        infill_tair : ndarray
            Time series of station observations with all observations replaced with
            values from the infill model regardless of whether or not an
            observation was originally missing.  
        '''
        nnghs = min_daily_nnghs
        
        trim_pca_tair = self.pca_tair[:, 0:1 + nnghs]
        trim_ngh_norms = self.ngh_norms[0:1 + nnghs]
        trim_ngh_std = self.ngh_std[0:1 + nnghs]
        
        engh_dly_nghs = self.__has_min_daily_nghs(nnghs,min_daily_nnghs)
        actual_nnghs = trim_pca_tair.shape[1] - 1
        
        while actual_nnghs < nnghs or not engh_dly_nghs:
        
            if actual_nnghs == nnghs and not engh_dly_nghs:
                
                nnghs += 1
            
            else:
                
                self.__extend_ngh_radius(MAX_DISTANCE / 2.0)

            trim_pca_tair = self.pca_tair[:, 0:1 + nnghs]
            trim_ngh_norms = self.ngh_norms[0:1 + nnghs]
            trim_ngh_std = self.ngh_std[0:1 + nnghs]
            engh_dly_nghs = self.__has_min_daily_nghs(nnghs,min_daily_nnghs)
            actual_nnghs = trim_pca_tair.shape[1] - 1
        
        #############################################################
        nnr_tair = self.nnr_ds.get_nngh_matrix(self.stn[LON],self.stn[LAT],self.tair_var,utc_offset=self.stn[UTC_OFFSET],nngh=nnghs_nnr)

        pc_loads, pc_scores, var_explain = pca_svd(nnr_tair, True, True)
        cusum_var = np.cumsum(var_explain)
    
        i = np.nonzero(cusum_var >= max_nnr_var)[0][0]
        
        nnr_tair = pc_scores[:,0:i+1]
        
        trim_pca_tair, trim_ngh_norms, trim_ngh_std = _shrink_matrix(trim_pca_tair, trim_ngh_norms, trim_ngh_std, min_daily_nnghs)
        
        if nnr_tair.size > 0:
        
            nnr_norms = np.mean(nnr_tair,dtype=np.float,axis=0)
            nnr_std = np.std(nnr_tair,dtype=np.float,axis=0,ddof=1)
            
            trim_pca_tair = np.hstack((trim_pca_tair,nnr_tair))
            trim_ngh_norms = np.concatenate((trim_ngh_norms,nnr_norms))
            trim_ngh_std = np.concatenate((trim_ngh_std,nnr_std))
        ############################################################

        ppca_rslt = r.ppca_tair(robjects.Matrix(trim_pca_tair), 
                                robjects.FloatVector(trim_ngh_norms),
                                robjects.FloatVector(trim_ngh_std),
                                frac_obs=frac_obs_initnpcs,
                                max_r2cum=ppca_varyexplain,
                                npcs=npcs,
                                convThres=ppcaConThres,
                                verbose=verbose)
        
        infill_tair = np.array(ppca_rslt.rx('ppca_fit'))
        infill_tair.shape = (infill_tair.shape[1],)
        npcsr = ppca_rslt.rx('npcs')[0][0]
        
        #############################################################
    
        obs_tair = trim_pca_tair[:, 0]
        
        if chk_perf:
            
            badImp,reasons = _is_badimp(infill_tair, self)
            
            if badImp:
                
                print "".join(["WARNING|",self.stn_id," had bad infill for ",
                               self.tair_var,". Reasons: ",reasons,
                               ". Retrying..."])
                
                if MIN_NNR_VAR < max_nnr_var:
                    impute_tair2, obs_tair2, npcsr2, nnghs2 = self.infill(min_daily_nnghs, nnghs_nnr, MIN_NNR_VAR, False, npcs, frac_obs_initnpcs, ppca_varyexplain,ppcaConThres)[0:4]    
                    badImp,reasons = _is_badimp(impute_tair2, self)
                
                if badImp:
                
                    newThres = [1e-6,1e-7]
                    
                    for aThres in newThres:
                    
                        impute_tair2, obs_tair2, npcsr2, nnghs2 = self.infill(min_daily_nnghs, nnghs_nnr, max_nnr_var, False, npcs, frac_obs_initnpcs, ppca_varyexplain,aThres)[0:4]
                                    
                        badImp,reasons = _is_badimp(impute_tair2, self)
                        
                        if not badImp:
                            break
                
                if badImp:
                    
                    print "".join(["ERROR|",self.stn_id," had bad infill for ",
                                   self.tair_var," even after retries. Reasons: ",reasons])
                
                else:
                    
                    print "".join(["SUCCESS INFILL RETRY|",self.stn_id," fixed bad infill for ",self.tair_var])
                    
                    infill_tair, obs_tair, npcsr, nnghs = impute_tair2, obs_tair2, npcsr2, nnghs2
        
        fnl_tair = np.copy(obs_tair)
        mask_infill = np.isnan(fnl_tair)
        fnl_tair[mask_infill] = infill_tair[mask_infill]
        
        return fnl_tair, mask_infill, infill_tair

def _is_badimp(impTair,impMatrix):
    
    rerun_imp = False
    reasons = ''
    
    obs_tair = impMatrix.pca_tair[:,0]
    chk_obs = obs_tair[impMatrix.valid_pca_mask[:,0]]
    chk_fit = impTair[impMatrix.valid_pca_mask[:,0]]
    mae = np.mean(np.abs(chk_fit-chk_obs))
    r_value = stats.linregress(chk_obs, chk_fit)[2]
    var_pct = r_value**2 #r-squared value; variance explained
    
    hasVarChgPt = r.hasVarChgPt(robjects.FloatVector(impTair))[0]
        
    #check for low infill performance
    if mae > 2.0 or var_pct < 0.7:
        
        rerun_imp = True
        reasons = "|".join([reasons,"low infill performance (%.2f,%.2f)"%(mae,var_pct)])
    
    #Check for extreme values
    if np.sum(impTair > 57.7) > 0 or np.sum(impTair < -89.4) > 0:
        
        rerun_imp = True
        reasons = "|".join([reasons,"impossible infill values"])
    
    #Check for variance change point
    if hasVarChgPt:
        
        rerun_imp = True
        reasons = "|".join([reasons,"variance change point"])
    
    return rerun_imp,reasons
    
    

def _shrink_matrix(aMatrix, nghNorms, nghStd, minNghs):
    '''
    After top minNghs stations, if a neighboring station time series
    does not add observations on days with < minNghs, remove
    it from the matrix.  
    '''
    
    validMask = np.isfinite(aMatrix[:,1:minNghs+1])
    nObs = np.sum(validMask,axis=1)
    maskBelowMin = nObs < minNghs
    
    keepCol = np.ones(aMatrix.shape[1],dtype=np.bool)
    
    for x in np.arange(minNghs+1,aMatrix.shape[1]):
        
        aCol = aMatrix[:,x]
        aColValidMask = np.isfinite(aCol)
        
        if np.sum(np.logical_and(maskBelowMin,aColValidMask)) > 0:
            aColValidMask.shape = (aColValidMask.size,1)
            validMask = np.hstack((validMask,aColValidMask))
            nObs = np.sum(validMask,axis=1)
            maskBelowMin = nObs < minNghs
        else:
            keepCol[x] = False
        
    return aMatrix[:,keepCol],nghNorms[keepCol],nghStd[keepCol] 

   
def _calc_ioa(x, y):
    '''
    Calculate the index of agreement (Durre et al. 2010; Legates and McCabe 1999) between x and y
    '''
    
    y_mean = np.mean(y)
    d = np.sum(np.abs(x - y_mean) + np.abs(y - y_mean))
    
    if d == 0.0:
        print "|".join(["WARNING: _calc_ioa: x, y identical"])
        #The x and y series are exactly the same
        #Return a perfect ioa
        return 1.0
    
    ioa = 1.0 - (np.sum(np.abs(y - x)) / d)
    
#    if ioa == 0:
#        print "|".join(["WARNING: _calc_ioa: ioa == 0"])
#        #Means all ys are the same or only one observation.
#        #This could possibly happen with prcp in arid regions
#        #Add on an extra observation to the time series that has same difference as x[0] and y[0]
#        x_new = np.concatenate([x, np.array([x[0] + (x[0] * .1)])])
#        y_new = np.concatenate([y, np.array([y[0] + (x[0] * .1)])])
#        
#        y_mean = np.mean(y_new)
#        ioa = 1.0 - (np.sum(np.abs(y_new - x_new)) / np.sum(np.abs(x_new - y_mean) + np.abs(y_new - y_mean)))  
    
    return ioa

def _load_R():

    global R_LOADED

    if not R_LOADED:

        path_root = os.path.dirname(__file__)
        fpath_rscript = os.path.join(path_root, 'rpy', 'pca_infill.R')
        r.source(fpath_rscript)
        R_LOADED = True
