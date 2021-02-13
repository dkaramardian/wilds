import os
import torch
import pandas as pd
import numpy as np
from wilds.datasets.wilds_dataset import WILDSDataset
from wilds.common.metrics.all_metrics import Accuracy, PrecisionAtRecall
from wilds.common.grouper import CombinatorialGrouper
from wilds.common.utils import subsample_idxs, threshold_at_recall
import torch.nn.functional as F

class SQFDataset(WILDSDataset):
    """
    New york stop and frisk data. CPW (weapons stops) from 2009 - 2012, as orginally provided by the NYPD and later
    cleaned by Goel, Rao, and Shroff 2016 https://projecteuclid.org/euclid.aoas/1458909920 . Shared with permission.
    https://5harad.com/#research for the full dataset.

     Supported `split_scheme`:
        'black', 'all_race', 'bronx', or 'all_borough'

     Input (x):
     Either 29 one-hot pre-stop observable features or 104=29 observables + 75 one-hot district indicators

     Label (y):
        y is binary. It is 1 if the stop is listed as finding a weapon, 0 otherwise.

    Metadata:
        Each stop is annotated with the borough the stop took place, the race of the stopped person, and whether the stop
        took place in the early or later time periond

    Website:
        NYPD - https://www1.nyc.gov/site/nypd/stats/reports-analysis/stopfrisk.page
        Cleaned data - https://5harad.com/data/sqf.RData

    Cleaning and analysis citation:
        @article{goel_precinct_2016,
            title = {Precinct or prejudice? {Understanding} racial disparities in {New} {York} {City}’s stop-and-frisk policy},
            volume = {10},
            issn = {1932-6157},
            shorttitle = {Precinct or prejudice?},
            url = {http://projecteuclid.org/euclid.aoas/1458909920},
            doi = {10.1214/15-AOAS897},
            language = {en},
            number = {1},
            journal = {The Annals of Applied Statistics},
            author = {Goel, Sharad and Rao, Justin M. and Shroff, Ravi},
            month = mar,
            year = {2016},
            pages = {365--394},
        }
    """
    def __init__(self, root_dir, download, split_scheme):
        # set variables
        self._dataset_name = 'sqf'
        self._version = '1.0'
        self._split_scheme = split_scheme
        self._y_size = 1
        self._n_classes = 2
        self._download_url = 'https://worksheets.codalab.org/rest/bundles/0xea27fd7daef642d2aa95b02f1e3ac404/contents/blob/'
        # path
        self._data_dir = self.initialize_data_dir(root_dir, download)

        # Load data
        data_df = pd.read_csv(os.path.join(self.data_dir, 'sqf.csv') , index_col=0)
        data_df = data_df[data_df['suspected.crime'] == 'cpw']
        categories = ['black', 'white hispanic', 'black hispanic', 'hispanic', 'white']
        data_df = data_df.loc[data_df['suspect.race'].map(lambda x: x in categories)]
        data_df['suspect.race'] = data_df['suspect.race'].map(lambda x: 'Hispanic' if 'hispanic' in x else x.title())

        # Only track weapons stops
        data_df = data_df[data_df['suspected.crime']=='cpw']

        # Get district features if measuring race, don't if measuring boroughs
        self.feats_to_use  = self.get_split_features(data_df.columns)

        # Drop data that doesn't have the all of the predictive features. This preserves almost all rows.
        data_df = data_df.dropna(subset=self.feats_to_use)

        # Get indices based on new index / after dropping rows with missing data
        train_idxs, test_idxs, val_idxs = self.get_split_indices(data_df)

        # Drop rows with unused metadata categories
        data_df = data_df.loc[train_idxs + test_idxs + val_idxs]

        # Reindex for simplicity
        data_df.index = range(data_df.shape[0])
        train_idxs = range(0, len(train_idxs))
        test_idxs = range(len(train_idxs), len(train_idxs)+ len(test_idxs))
        val_idxs = range(test_idxs[-1], data_df.shape[0] )

        # Normalize continuous features
        data_df = self.normalize_data(data_df, train_idxs)
        self._input_array = data_df

        # Create split dictionaries
        self.initialize_split_dicts()

        # Get whether a weapon was found for various groups
        self._y_array = torch.from_numpy(data_df['found.weapon'].values).long()

        # Metadata will be int dicts
        self._identity_vars = [ 'suspect.race', 'borough', 'train.period']

        explicit_identity_label_df, self._metadata_map = self.load_metadata(data_df)

        self._metadata_array = torch.cat(
            (
                torch.LongTensor(explicit_identity_label_df.values),
                self._y_array.reshape((-1, 1))
            ),
            dim=1
        )
        self._metadata_fields = ['suspect race', 'borough', '2010 or earlier?'] + ['y']

        self.get_split_maps( data_df,  train_idxs, test_idxs, val_idxs)

        data_df = data_df[self.feats_to_use]
        self._input_array = pd.get_dummies(data_df, columns=[i for i in self.feats_to_use if 'suspect.' not in i and
                                                             'observation.period' not in i], drop_first=True)
        # Recover relevant features after taking dummies
        new_feats = []
        for i in self.feats_to_use:
            for j in self._input_array:
                if i in j:
                    new_feats.append(j)
                else:
                    pass
        self._input_array = self._input_array[new_feats]
        self.initialize_eval_grouper()

    def load_metadata(self, data_df):
        metadata_df = data_df[self._identity_vars].copy()
        metadata_names = ['suspect race', 'borough', '2010 or earlier?']
        metadata_ordered_maps = {}
        for col_name, meta_name in zip(metadata_df.columns, metadata_names):

            col_order = sorted(set(metadata_df[col_name]))
            col_dict = dict(zip(col_order, range(len(col_order))))
            metadata_ordered_maps[col_name] = col_order
            metadata_df[meta_name] = metadata_df[col_name].map(col_dict)
        return metadata_df[metadata_names], metadata_ordered_maps


    def get_split_indices(self, data_df):
        """Finds splits based on the split type """
        test_idxs =  data_df[data_df.year > 2010].index.tolist()
        train_df = data_df[data_df.year <= 2010]
        validation_id_idxs = subsample_idxs(train_df.index.tolist(), num=int(train_df.shape[0] * 0.2),  seed=2851,
                                            take_rest=False)

        train_df = train_df[~train_df.index.isin(validation_id_idxs)]

        if 'black' == self._split_scheme:
            train_idxs = train_df[train_df['suspect.race'] == 'Black'].index.tolist()

        elif 'all_race' in self._split_scheme:
            black_train_size = train_df[train_df['suspect.race'] == 'Black'].shape[0]
            train_idxs = subsample_idxs(train_df.index.tolist(), num=black_train_size, take_rest=False, seed=4999)

        elif 'all_borough' == self._split_scheme:
            bronx_train_size = train_df[train_df['borough'] == 'Bronx'].shape[0]
            train_idxs = subsample_idxs(train_df.index.tolist(), num=bronx_train_size, take_rest=False, seed=8614)

        elif 'bronx' == self._split_scheme:
            train_idxs = train_df[train_df['borough'] == 'Bronx'].index.tolist()

        else:
            raise ValueError(f'Split scheme {self.split_scheme} not recognized')

        return train_idxs, test_idxs, validation_id_idxs

    def indices_to_dict(self, indices, int_val):
        local_idx_dict = {}
        for i in indices:
            local_idx_dict[i] = int_val
        return local_idx_dict

    def get_split_maps(self, data_df,  train_idxs, test_idxs, val_idxs):
        """Using the existing split indices, create a map to put entries to training and validation sets. """
        self._split_array = np.zeros(data_df.shape[0])
        self._split_array[train_idxs] = 0
        self._split_array[test_idxs] = 1
        self._split_array[val_idxs] = 2

    def get_split_features(self, columns):
        """Get features that include precinct if we're splitting on race or don't include if we're using borough splits."""
        feats_to_use = []
        if 'bronx' not in self._split_scheme and 'borough' not in self._split_scheme:
            feats_to_use.append('precinct')

        feats_to_use += ['suspect.height', 'suspect.weight', 'suspect.age', 'observation.period',
                        'inside.outside', 'location.housing', 'radio.run', 'officer.uniform']
        # Primary stop reasoning features
        feats_to_use += [i for i in columns if 'stopped.bc' in i]
        # Secondary stop reasoning features, if any
        feats_to_use += [i for i in columns if 'additional' in i]

        return feats_to_use

    def normalize_data(self, df,  train_idxs):
        """"Normalizes the data as Goel et al do - continuous features only"""
        columns_to_norm = ['suspect.height', 'suspect.weight', 'suspect.age', 'observation.period']
        df_unnormed_train = df.loc[train_idxs].copy()
        for feature_name in columns_to_norm:
            df[feature_name] = df[feature_name] - np.mean(df_unnormed_train[feature_name])
            df[feature_name] = df[feature_name] / np.std(df_unnormed_train[feature_name])
        return df


    def initialize_split_dicts(self):
        """Identify split indices and name splits"""
        if 'all_borough' == self.split_scheme :
            self._split_dict = {'train': 0, 'test': 1, 'val':2}
            self._split_names = {'train': 'All Boroughs 2009 & 10  subsampled to match Bronx train set size', 'test':'All Stops 2010 & 11', \
                                 'val':'20% sample of all stops 2009 & 10'}
        elif 'bronx' == self.split_scheme:
                self._split_dict = {'train': 0, 'test': 1, 'val': 2}
                self._split_names = {'train': 'Bronx 2009 & 10', 'test': 'All Stops 2010 & 11', \
                                     'val': '20% sample of all stops 2009 & 10'}
        elif 'black' == self.split_scheme:
            self._split_dict = {'train': 0, 'test': 1, 'val':2}
            self._split_names = {'train': 'train: 80% Black Stops 2009 and 2010', 'test':'Test: All Stops 2011 and 2012. ', \
                                 'val':'20% sample of all stops 2009 & 10'}
        elif 'all_race' == self.split_scheme :
            self._split_dict = {'train': 0, 'test': 1, 'val':2}
            self._split_names = {'train': 'train: Stops 2009 and 2010 subsampled to the size of Black people training set', 'test':'Test: All Stops 2011 and 2012. ', \
                                 'val':'20% sample of all stops 2009 & 10'}
        else:
            raise ValueError(f'Split scheme {self.split_scheme} not recognized')


    def get_input(self, idx):
        return torch.FloatTensor(self._input_array.loc[idx].values)

    def eval(self, y_pred, y_true, metadata):
        """Evaluate the precision achieve overall and across groups for a given global recall"""
        g = self._eval_grouper.metadata_to_group(metadata)

        y_scores = F.softmax(y_pred, dim=1)[:,1]
        threshold_60 = threshold_at_recall(y_scores, y_true)
        results = Accuracy().compute(y_pred, y_true)
        results.update(PrecisionAtRecall(threshold_60).compute(y_pred, y_true))
        results.update(Accuracy().compute_group_wise(y_pred, y_true, g, self._eval_grouper.n_groups))
        results.update(
        PrecisionAtRecall(threshold_60).compute_group_wise(y_pred, y_true, g, self._eval_grouper.n_groups))

        results_str = (
            f"Average {PrecisionAtRecall(threshold=threshold_60).name }:  {results[PrecisionAtRecall(threshold=threshold_60).agg_metric_field]:.3f}\n"
            f"Average {Accuracy().name}:  {results[Accuracy().agg_metric_field]:.3f}\n"
        )

        return results, results_str

    def initialize_eval_grouper(self):
        if 'black' in self.split_scheme or 'race' in self.split_scheme :
            self._eval_grouper = CombinatorialGrouper(
                dataset=self,
                groupby_fields = ['suspect race']
            )
        elif 'bronx' in self.split_scheme or 'all_borough' == self.split_scheme:
            self._eval_grouper = CombinatorialGrouper(
                dataset=self,
                groupby_fields = ['borough'])
        else:
            raise ValueError(f'Split scheme {self.split_scheme} not recognized')


