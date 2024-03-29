import re
import itertools
import os
import pandas as pd
import numpy as np
from scipy import stats
from prettytable import PrettyTable
from tqdm import tqdm


def get_char(seq):
    """split string int sequence of chars returned in pandas.Series"""
    chars = list(seq)
    return pd.Series(chars)

class SeqProcessConfig(object):
    def __init__(self, seq_len, seq_stend, ewindow_stend, offset_val):
        self.seq_len = seq_len
        # entries are with respect to offset value (i.e. start and end indices)
        self.seq_stend = seq_stend
        self.ewindow_stend = ewindow_stend
        self.offset_val = offset_val
        # determine the range (start and end) from the offset provided
        self._determine_offset_range()
        # map the indices to 0-based indexing
        self._translate_to_0based_indexing()
        
    def _determine_offset_range(self):
        # printing indices
        st = self.offset_val
        if st <= 0:
            # for example -4,25 (equivalent to 30 elements where 0 is included)
            end = self.seq_len - abs(st) - 1 
        else:
            # for example 1,30 (equivalent to 30 elements)
            end = self.seq_len + st - 1
        self.offset_st = st
        self.offset_end = end
        
    def _translate_to_0based_indexing(self):
        offset = self.offset_val
        # edit window mapping
        st, end = self.ewindow_stend
        self.ewindow_st = st - offset
        self.ewindow_end = end - offset
        
        # sequence mapping
        st, end = self.seq_stend
        self.seq_st = st - offset
        self.seq_end = end - offset
    
    def __str__(self):
        tb = PrettyTable()
        tb.field_names = ['Sequence processing Config', 'Value']
        tb.add_row(['sequence length', self.seq_len])
        tb.add_row(['sequence start index (0-based indexing)', self.seq_st])
        tb.add_row(['sequence end index (0-based indexing)', self.seq_end])
        tb.add_row(['editable window start index (0-based indexing)', self.ewindow_st])
        tb.add_row(['editable window end index (0-based indexing)', self.ewindow_end])
        tb.add_row(['offset start numbering', self.offset_st])
        tb.add_row(['offset end numbering', self.offset_end])
        return tb.get_string()

class HaplotypeSeqProcessor(object):
    def __init__(self, base_editor, conversion_nucl, seqconfig, max_num_targets=8):
        self.base_editor = base_editor
        self.conversion_nucl = conversion_nucl
        self.seqconfig = seqconfig
        self.max_num_targets = max_num_targets
        self.describe()
        
    def describe(self):
        tb = PrettyTable()
        tb.field_names = ['Description', 'Value']
        tb.add_row(['Base editor', self.base_editor])
        tb.add_row(['Target nucleotide', self.conversion_nucl[0]])
        tb.add_row(['Conversion nucleotide', self.conversion_nucl[1]])
        tb.add_row(['Maximum number of targets considered', self.max_num_targets])
        print(tb)
        print(self.seqconfig)

    def _determine_target_complem_nucl(self):
        tb_nucl, cb_nucl = self.conversion_nucl
        return tb_nucl, cb_nucl

    def remove_viol_seqs(self, df, inpseq_col):
        """
        Args:
            df: dataframe
            inpseq_col: string, column name of input sequence such as "Inp_seq"
        """
        print('--- checking for violating seqs ---')
        #print(inpseq_col)
        seq_df = df.copy()
        tb_nucl, __ = self.conversion_nucl
        seqlen = self.seqconfig.seq_len
        viol_seqs = []
        
        cond_letter = ~seq_df[inpseq_col].str.contains(tb_nucl)
        cond_len = ~seq_df[inpseq_col].str.len() == seqlen
        df_clean = seq_df
        if cond_len.any() or cond_letter.any():
            cond = cond_letter | cond_len
            print(seq_df.loc[cond, inpseq_col])
            df_clean = seq_df.loc[~cond].copy()
            df_clean.reset_index(inplace=True, drop=True)
   
        return df_clean

    def _check_duplicates(self, gdf, outcomeseq_colname, pbar, prg_counter):
        gdf_clean = gdf.copy()
        gdf_clean.drop_duplicates(subset=[outcomeseq_colname], inplace=True, ignore_index=True)
        prg_counter+=1
        pbar.update(prg_counter)
        return gdf_clean

    def preprocess_df(self, df, inpseq_colnames, outcomeseq_colname):
        """
        Args:
            df: dataframe
            inpseq_colnames: list of column names such as ['seq_id', 'Inp_seq']
            outcomeseq_colname: string, column name of observed outcome sequences
        
        """
        print('--- removing duplicates (if found!) ---')
        prg_counter=0
        dfg = df.groupby(by=inpseq_colnames)
        pbar = tqdm(total=dfg.ngroups)
        df_clean = dfg.apply(self._check_duplicates, outcomeseq_colname, pbar, prg_counter)
        pbar.close()
        df_clean.reset_index(inplace=True, drop=True)
        return df_clean


    def renormalize_outcome_prop(self, df, by_cols, prop_col):
        """ renormalize the outcome sequence probability (optional, in case it is not normalized!)
        Args:
            df:pd.DataFrame, read data frame
            by_cols: list, input sequence column name/s such as ['seq_id', 'Inp_seq']
            prop_col: string, outcome propotion (i.e. probability) column name

        .. Note:
            this method is run after using :func:`preprocess_df`
        """
        print('--- renormalizing outcome proportion ---')
        a = df.groupby(by=by_cols, as_index=False)[prop_col].sum()
        a['denom'] = a[prop_col]
        b = df.copy()
        b = b.merge(a, on=by_cols, how='left')
        validate_df(b)
        b['prob'] = b[f'{prop_col}_x']/b['denom']
        b[prop_col] = b['prob']
        return b

    def _generate_combinatorial_conversion(self, tbase_indices, conv_nl):
        num_pos = len(tbase_indices)
        comb_nucl_lst= []
        conv_nl_lst = list(conv_nl)
        #print(tbase_indices)
        for __ in range(num_pos):
            comb_nucl_lst.append(conv_nl_lst)
        #print(comb_nucl_lst)
        return itertools.product(*comb_nucl_lst)

    def generate_combinatorial_outcome(self, df):
        """ Generates combinatorial outcome sequences based on identified canonical bases

        Args:
            df:pd.DataFrame, processed dataframe using  :func:`process_inp_outp_df` function
        """
        print('--- generating edit combinations ---')
#         print(df.columns)
#         print(df.shape)
        
        seqconfig = self.seqconfig
        conv_nl = self.conversion_nucl
        tb_nucl, cb_nucl = conv_nl
        e_st = seqconfig.ewindow_st
        e_end = seqconfig.ewindow_end
        seqlen = seqconfig.seq_len
        max_num_targets=self.max_num_targets
        res_df_lst = []
        # these are already generated in process_inp_outp_df method (so it is safe to use as predefined column names)
        target_cols = ['seq_id', 'Inp_seq', 'Outp_seq']

        for row in tqdm(df.iterrows()):
            
            indx, record = row
            rec_nucl = record[[f'Inp_L{i}'for i in range(e_st+1,e_end+2)]]
            # print('indx:', indx)
#             print(rec_nucl)
       
            tbase_indices = np.where(rec_nucl==tb_nucl)[0]
            #print('tbase_indices:\n', tbase_indices)
            
            if len(tbase_indices) > max_num_targets:
                tbase_indices = tbase_indices[:max_num_targets]

                
#             print('e_st:', e_st)
#             print('e_end:', e_end)
#             print('tbase_indices:\n', tbase_indices)
            
            comb_nucl_opt= self._generate_combinatorial_conversion(tbase_indices, conv_nl)
            
            comb_nucl_opt = list(comb_nucl_opt)
            num_options = len(comb_nucl_opt)
            #print(num_options)
            comb_nucl_arr = np.repeat(rec_nucl.values.reshape(1,-1),num_options,axis=0)
#             print(comb_nucl_arr)
            for i_arr, opt in enumerate(comb_nucl_opt):
                # print('i_arr:', i_arr)
                # print('opt:',opt)
                comb_nucl_arr[i_arr, tbase_indices]= opt
#             print(comb_nucl_arr)
            comb_nucl_df = pd.DataFrame(comb_nucl_arr)
            comb_nucl_df.columns = [f'Inp_L{i}'for i in range(e_st+1,e_end+2)]
#             print(comb_nucl_df)
            
            pre_ew_col = record[[f'Inp_L{i}'for i in range(1,e_st+1)]]
            post_ew_col = record[[f'Inp_L{i}'for i in range(e_end+2,seqlen+1)]]
            
            a = pd.DataFrame(np.repeat(pre_ew_col.values.reshape(1,-1), num_options, axis=0))
            a.columns = [f'Inp_L{i}'for i in range(1,e_st+1)]
            # print(a)
            
            b = pd.DataFrame(np.repeat(post_ew_col.values.reshape(1,-1), num_options, axis=0))
            b.columns = [f'Inp_L{i}'for i in range(e_end+2,seqlen+1)]
            # print(b)
            
            # print(record['Inp_seq'])
            inpseq_df = pd.DataFrame([record['Inp_seq']]*num_options)
            
            inpseq_df.columns = ['Inp_seq']
            
            seqid_df = pd.DataFrame([record['seq_id']]*num_options)
        
            seqid_df.columns = ['seq_id']
            
            res_df = pd.concat([seqid_df,inpseq_df, a, comb_nucl_df, b], axis=1)
     
            # print(res_df)
            
            res_df['Outp_seq'] = res_df[[f'Inp_L{i}'for i in range(1,seqlen+1)]].astype(str).sum(axis=1)
            # print(res_df)
            res_df_lst.append(res_df[target_cols])
            #print(res_df_lst)
            # print('-'*15)
       
        comb_final_df = pd.concat(res_df_lst, axis=0,ignore_index=True)
        print('comb_final_df:\n', comb_final_df.columns)
        ## we need this only for twos-step model proprotional model
        df_new = comb_final_df.copy()
        df_new = df_new.drop(df_new[df_new["Inp_seq"] == df_new["Outp_seq"]].index)
        df_new = df_new.reset_index()
        return df_new

    
    def process_inp_outp_df(self, df, seqid_col, t_inp_col, t_outp_col, outcome_prop_col):
        """
        df:pd.DataFrame, read data frame
        t_inp_col: string, input sequence column name
        t_outp_col: string, output sequence column name
                    None, when performing inference
        outcome_prop_col: string, outcome propotion (i.e. probability of outcome sequence) column name
                          None, when performing inference
        """
#         print()
#         print('__ process_inp_outp __')
#         print('df.columns:', df.columns)
#         print()
        #print(t_inp_col)
        max_num_targets = self.max_num_targets
        #print('max', max_num_targets)
        pbar = tqdm(total=100)
        #seq_len = self.seqconfig.seq_len
        #print(df[t_inp_col])
        seq_len = len(df[t_inp_col][0])
        print('input sequence length:', seq_len)
        
        tb_nucl, cb_nucl = self._determine_target_complem_nucl()
        
        inp_df = self._process_df(df, seqid_col, t_inp_col, tb_nucl, 'Inp')
         
        if t_outp_col is not None:
            pbar.update(25)
            outp_df = self._process_df(df, seqid_col, t_outp_col, cb_nucl, 'Outp')
            pbar.update(50)
            conv_mat = inp_df[[f'Inp_M{i}' for i in range(1,seq_len+1)]].values & \
                       outp_df[[f'Outp_M{i}' for i in range(1,seq_len+1)]].values
            conv_df = pd.DataFrame(conv_mat)
            conv_df.columns = [f'conv{tb_nucl}{cb_nucl}_{i}' for i in range(1,seq_len+1)]
            pbar.update(75)
            if outcome_prop_col is not None:
                proc_df = pd.concat([inp_df, outp_df, conv_df, pd.DataFrame(df[outcome_prop_col])], axis=1)
            else:
                proc_df = pd.concat([inp_df, outp_df, conv_df], axis=1)

        else:
            pbar.update(50)
            proc_df = inp_df
            pbar.update(75)
  
        # remove double seq_id columns
        proc_df = proc_df.loc[:,~proc_df.columns.duplicated()]
        pbar.update(100)
        pbar.close()
        #print('proc_df.columns:', proc_df.columns)
        validate_df(proc_df)
#         print()
        #print('we finished')
        return proc_df
    
    def _get_char(self,seq):
        """split string int sequence of chars returned in pandas.Series"""
        chars = list(seq)
        return pd.Series(chars)
    
    def _process_df(self, df, seqid_col, tcol, target_base, suffix):
        """cleans a data frame representing sequences and their edit info obtained from crispr experiment

        Args:
            df: pandas.DataFrame
            tcol: string,
            target_base: string, 
            suffix: string,

        Note:
            assumed columns in the dataframe are:
        """
        ## process outcome sequences
#         print('__ process_df __')
#         print(df.columns)
        
        seqid_df = pd.DataFrame(df[seqid_col].copy())
        seqid_df.columns = ['seq_id']
        df = pd.DataFrame(df[tcol].copy())
        seq_colname = f'{suffix}_seq'
        df.columns = [seq_colname]
        # harmonize sequence string representation to capitalized form
        df[seq_colname] = df[seq_colname].str.upper()

        baseseq_df = df[seq_colname].apply(self._get_char)
        num_nucl = len(baseseq_df.columns)+1
        baseseq_df.columns = [f'{suffix}_B{i}' for  i in range(1, num_nucl)]

        base_mask = (baseseq_df == target_base) * 1
        base_mask.columns = [f'{suffix}_M{i}' for  i in range(1, num_nucl)]

        baseseq_letters_df = baseseq_df.copy()
        baseseq_letters_df.columns = [f'{suffix}_L{i}' for  i in range(1, num_nucl)]

        # replace base letters with numbers
        #mapping_dict = {'A': 0, 'C': 1, 'T': 2, 'G': 3}
        baseseq_df.replace(['A', 'C', 'T', 'G'], [0,1,2,3], inplace=True)
        #baseseq_df = baseseq_df.applymap(lambda x: mapping_dict.get(x, x))
       
        
        base_df = pd.concat([seqid_df,
                             base_mask,
                             df,
                             baseseq_letters_df,
                             baseseq_df], axis=1)
        base_df.reset_index(inplace=True, drop=True)
        return base_df
    
def validate_df(df):
    print('number of NA:', df.isna().any().sum())

class VizInpOutp_Haplotype(object):

    html_colors = {'blue':' #aed6f1',
                   'red':' #f5b7b1',
                   'green':' #a3e4d7',
                   'yellow':' #f9e79f',
                   'violet':'#d7bde2'}
    codes = {'A':'@', 'C':'!', 'T':'#', 'G':'_', 'conv':'~', 'prob':'%'}
    nucl_colrmap = {'A':'red',
                   'C':'yellow',
                   'T':'blue',
                   'G':'green',
                   'prob':'violet'}
    
    def __init__(self):
        pass
 

    @classmethod
    def viz_align_haplotype(clss, df, seqid, outcome_colname, seqconfig, conv_nl, predscore_thr=0., return_type='html'):
        """
        Args:
            df: processed dataframe using HaplotypeSeqProcessor.process_inp_outp_df
            seqid: string, sequence id column name
            outcome_colname: string or None, the ground truth outcome proportion column name
            seqconfig: instance of SeqProcessConfig class
            conv_nl: tuple of (target nucleotide, transition nucleotide)
            predscore_thr: float, probability threshold 
            return_type: string, default `html`
        
        """
        seq_len = seqconfig.seq_len
        seq_st, seq_end = seqconfig.seq_st, seqconfig.seq_end
        ewindow_st, ewindow_end = seqconfig.ewindow_st, seqconfig.ewindow_end
        offset_st, offset_end = seqconfig.offset_st, seqconfig.offset_end
        
        tb_nucl, cb_nucl = conv_nl
        codes = clss.codes
        
        tb = PrettyTable()
        tb.field_names = ['Desc.'] + [f'{i}' for i in range(1, seq_len+1)]
        
        cond = df['seq_id'] == seqid
        if 'pred_score' in df:
            cond_thr = df['pred_score'] >= predscore_thr
            cond_combined = (cond) & (cond_thr)
        else:
            cond_combined = cond
        df = df.loc[cond_combined].copy()
        # sort df by outcome probability
        if outcome_colname is not None:
            df.sort_values(by=[outcome_colname], ascending=False, inplace=True)
        else:
            df.sort_values(by=['pred_score'], ascending=False, inplace=True)

        # get the input sequence
        inp_nucl = df.iloc[0][[f'Inp_L{i}' for i in range(1,seq_len+1)]].values
        inp_str_lst = ['Input sequence'] + [f'{codes[nucl]}{nucl}' for nucl in inp_nucl]
        tb.add_row(inp_str_lst)

        n_rows = df.shape[0]
        # generate outcome (haplotype) rows
        for rcounter in range(n_rows):
            row = df.iloc[rcounter]
            outp_nucl = row[[f'Outp_L{i}' for i in range(1,seq_len+1)]].values
            if outcome_colname is not None:
                outp_str_lst = ['{}Output sequence\n Prob.={:.4f}'.format(codes['prob'], row[outcome_colname])]
            else:
                outp_str_lst = ['{}Output sequence'.format(codes['prob'])]

            cl_lst = []
            for pos, nucl in enumerate(outp_nucl):
                if row[f'conv{tb_nucl}{cb_nucl}_{pos+1}']:
                    cl_lst += [f"{codes['conv']}{nucl}"]
                else:
                    cl_lst += [f'{nucl}']
            outp_str_lst += cl_lst
            tb.add_row(outp_str_lst)

        pos_str_lst = ['Position numbering']+[str(elm) for elm in range(offset_st, offset_end+1)]
        tb.add_row(pos_str_lst)

        ewindow_str_lst = ['Editable window (*)'] + \
                      [' ' for elm in range(0, ewindow_st)]+ \
                      ['*' for elm in range(ewindow_st, ewindow_end+1)]+ \
                      [' ' for elm in range(ewindow_end+1, seq_len)]
        tb.add_row(ewindow_str_lst)

        seqwindow_str_lst = ['Sequence window (+)'] + \
                      [' ' for elm in range(0, seq_st)]+ \
                      ['+' for elm in range(seq_st, seq_end+1)]+ \
                      [' ' for elm in range(seq_end+1, seq_len)]
        tb.add_row(seqwindow_str_lst)

        if return_type == 'html':
            return clss._format_html_table(tb.get_html_string(), conv_nl)
        else: # default string
            return tb.get_string()
    @classmethod
    def _format_html_table(clss, html_str, conv_nl):
        tb_nucl, cb_nucl = conv_nl
        html_colors = clss.html_colors
        codes = clss.codes
        nucl_colrmap = clss.nucl_colrmap
        for nucl in codes:
            if nucl == 'conv':
                ctext = codes[nucl]
                color = html_colors[nucl_colrmap[cb_nucl]]
            else:
                ctext = codes[nucl]
                color = html_colors[nucl_colrmap[nucl]]
            html_str = re.sub(f'<td>{ctext}', '<td bgcolor="{}">'.format(color), html_str)
        return html_str

class HaplotypeVizFile():
    def __init__(self, resc_pth):
        # resc_pth: viz resources folder path
        # it contains 'header.txt',  'jupcellstyle.css', 'begin.txt', and 'end.txt'
        self.resc_pth = resc_pth
    def create(self, tablehtml, dest_pth, fname):
        resc_pth = self.resc_pth
        ls = []
        for ftname in ('header.txt',  'jupcellstyle.css', 'begin.txt'):
            with open(os.path.join(resc_pth, ftname), mode='r') as f:
                ls.extend(f.readlines())
        ls.append(tablehtml)
        with open(os.path.join(resc_pth, 'end.txt'), mode='r') as f:
            ls.extend(f.readlines())
        content = "".join(ls)
        with open(os.path.join(dest_pth, f'{fname}.html'), mode='w') as f:
            f.write(content)