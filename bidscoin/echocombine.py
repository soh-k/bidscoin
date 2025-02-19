#!/usr/bin/env python3
"""
A wrapper around the 'mecombine' multi-echo combination tool (https://github.com/Donders-Institute/multiecho).

Except for BIDS inheritances, this wrapper is BIDS-aware (a 'bidsapp') and writes BIDS compliant output
"""

import argparse
import json
import logging
import pandas as pd
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from multiecho import combination as me
from pathlib import Path
try:
    from bidscoin import bidscoin, bids
except ImportError:
    import bidscoin, bids             # This should work if bidscoin was not pip-installed

unknowndatatype = 'extra_data'

def echocombine(bidsdir: str, pattern: str, subjects: list, output: str, algorithm: str, weights: list, force: bool=False):
    """

    :param bidsdir:     The bids-directory with the (multi-echo) subject data
    :param pattern:     Globlike recursive search pattern (relative to the subject/session folder) to select the first echo of the images that need to be combined, e.g. '*task-*echo-1*'
    :param subjects:    List of sub-# identifiers to be processed (the sub- prefix can be left out). If not specified then all sub-folders in the bidsfolder will be processed
    :param output:      Determines where the output is saved. It can be the name of a BIDS datatype folder, such as 'func', or of the derivatives folder, i.e. 'derivatives'. If output = [the name of the input datatype folder] then the original echo images are replaced by one combined image. If output is left empty then the combined image is saved in the input datatype folder and the original echo images are moved to the {unknowndatatype} folder
    :param algorithm:   Combination algorithm, either 'PAID', 'TE' or 'average'
    :param weights:     Weights for each echo
    :param force:       Boolean to overwrite existing ME target files
    :return:
    """

    # Input checking
    bidsdir = Path(bidsdir).resolve()

    # Start logging
    bidscoin.setup_logging(bidsdir/'code'/'bidscoin'/'echocombine.log')
    LOGGER.info('')
    LOGGER.info(f"--------- START echocombine ---------")
    LOGGER.info(f">>> echocombine bidsfolder={bidsdir} pattern={pattern} subjects={subjects} output={output}"
                f" algorithm={algorithm} weights={weights}")

    # Get the list of subjects
    if not subjects:
        subjects = bidscoin.lsdirs(bidsdir, 'sub-*')
        if not subjects:
            LOGGER.warning(f"No subjects found in: {bidsdir/'sub-*'}")
    else:
        subjects = ['sub-' + subject.replace('sub-', '') for subject in subjects]              # Make sure there is a "sub-" prefix
        subjects = [bidsdir/subject for subject in subjects if (bidsdir/subject).is_dir()]

    # Loop over bids subject/session-directories
    with logging_redirect_tqdm():
        for n, subject in enumerate(tqdm(subjects, unit='subject', leave=False), 1):

            sessions = bidscoin.lsdirs(subject, 'ses-*')
            if not sessions:
                sessions = [subject]
            for session in sessions:

                LOGGER.info('-------------------------------------')
                LOGGER.info(f"Combining echos for ({n}/{len(subjects)}): {session}")

                subid, sesid = bids.DataSource(session/'dum.my', subprefix='sub-', sesprefix='ses-').subid_sesid()

                # Search for multi-echo matches
                for match in sorted([match for match in session.rglob(pattern) if '.nii' in match.suffixes]):

                    # Check if it is normal/BIDS multi-echo data or that the echo-number is appended to the acquisition label (as done in BIDScoin)
                    if '_echo-' in match.name:
                        echonr      = bids.get_bidsvalue(match, 'echo')
                        mepattern   = bids.get_bidsvalue(match, 'echo', '*')                        # The pattern that selects all echos
                        cename      = match.name.replace(f"_echo-{echonr}", '')                     # The combined-echo output filename
                    elif '_acq-' in match.name and bids.get_bidsvalue(match, 'acq').split('e')[-1].isnumeric():
                        acq, echonr = bids.get_bidsvalue(match, 'acq').rsplit('e',1)
                        mepattern   = bids.get_bidsvalue(match, 'acq', acq + 'e*')                  # The pattern that selects all echos
                        cename      = match.name.replace(f"_acq-{acq}e{echonr}", f"_acq-{acq}")     # The combined-echo output filename
                        LOGGER.info(f"No 'echo' key-value pair found in the filename, using the 'acq-{acq}e{echonr}' pair instead (BIDScoin-style)")
                    else:
                        LOGGER.warning(f"No 'echo' encoding found in the filename, skipping: {match}")
                        continue
                    echos     = sorted(match.parent.glob(mepattern.name))
                    newechos  = [echo.parents[1]/unknowndatatype/echo.name for echo in echos]
                    if len(echos) == 1:
                        LOGGER.warning(f"Only one echo image found, nothing to do for: {match}")
                        continue

                    # Construct the combined-echo output filename and check if that file already exists
                    datatype = match.parent.name
                    if not output:
                        cefile = session/datatype/cename
                    elif output == 'derivatives':
                        cefile = bidsdir/'derivatives'/'multiecho'/subid/sesid/datatype/cename
                    else:
                        cefile = session/output/cename
                    cefile.parent.mkdir(parents=True, exist_ok=True)
                    if cefile.is_file() and not force:
                        LOGGER.warning(f"Outputfile {cefile} already exists, skipping: {match}")
                        continue

                    # Combine the multi-echo images
                    me.me_combine(mepattern, cefile, algorithm, weights, saveweights=False)

                    # (Re)move the original multi-echo images
                    if not output:
                        for echo, newecho in zip(echos, newechos):
                            LOGGER.info(f"Moving original echo image: {echo} -> {newecho}")
                            newecho.parent.mkdir(parents=True, exist_ok=True)
                            echo.replace(newecho)
                            echo.with_suffix('').with_suffix('.json').replace(newecho.with_suffix('').with_suffix('.json'))
                    elif output == datatype:
                        for echo in echos:
                            LOGGER.info(f"Removing original echo image: {echo}")
                            echo.unlink()
                            echo.with_suffix('').with_suffix('.json').unlink()

                    # Construct the path names relative to the session folder (as in the scans.tsv file)
                    oldechos_rel = [echo.relative_to(session).as_posix() for echo in echos]
                    newechos_rel = [echo.relative_to(session).as_posix() for echo in echos + newechos if echo.is_file()]
                    if output == 'derivatives':
                        cefile_rel = ''                 # A remote folder cannot be specified as IntendedFor :-(
                    else:
                        cefile_rel = cefile.relative_to(session).as_posix()

                    # Update the IntendedFor fields of the fieldmaps (i.e. remove the old echos, add the echo-combined image and, optionally, the new echos)
                    if output != 'derivatives' and (session/'fmap').is_dir():
                        for fmap in (session/'fmap').glob('*.json'):
                            with fmap.open('r') as fmap_fid:
                                metadata = json.load(fmap_fid)
                            intendedfor = metadata.get('IntendedFor', [])
                            if isinstance(intendedfor, str):
                                intendedfor = [intendedfor]
                            if sesid:                   # NB: IntendedFor is relative to the subject folder
                                intendedfor = [file.split(sesid+'/',1)[1] for file in intendedfor]
                            if oldechos_rel[0] in intendedfor:
                                LOGGER.info(f"Updating 'IntendedFor' in {fmap}")
                                relfiles                = [file for file in intendedfor if file not in oldechos_rel] + newechos_rel + [cefile_rel]
                                metadata['IntendedFor'] = [(Path(sesid)/relfile).as_posix() for relfile in relfiles]
                                with fmap.open('w') as fmap_fid:
                                    json.dump(metadata, fmap_fid, indent=4)

                    # Update the scans.tsv file
                    if (bidsdir/'.bidsignore').is_file():
                        bidsignore = (bidsdir/'.bidsignore').read_text().splitlines()
                    else:
                        bidsignore = [unknowndatatype + '/']
                    scans_tsv = session/f"{subid}{bids.add_prefix('_', sesid)}_scans.tsv"
                    if scans_tsv.is_file():

                        scans_table = pd.read_csv(scans_tsv, sep='\t', index_col='filename')
                        if oldechos_rel[0] in scans_table.index:
                            scans_table.loc['oldrow'] = scans_table.loc[oldechos_rel[0]]
                        elif 'acq_time' in scans_table:
                            with cefile.with_suffix('').with_suffix('.json').open('r') as fid:
                                metadata = json.load(fid)
                            date = scans_table.iloc[0]['acq_time'].split('T')[0]
                            scans_table.loc['oldrow', 'acq_time'] = f"{date}T{metadata.get('AcquisitionTime')}"
                        else:
                            scans_table.loc['oldrow'] = None

                        if output+'/' not in bidsignore + ['derivatives/'] and cefile.parent.name in bids.bidsdatatypes:
                            LOGGER.info(f"Adding '{cefile_rel}' to '{scans_tsv}'")
                            scans_table.loc[cefile_rel] = scans_table.loc['oldrow']

                        for echo in oldechos_rel + newechos_rel:
                            if echo in scans_table.index and not (session/echo).is_file():
                                LOGGER.info(f"Removing '{echo}' from '{scans_tsv}'")
                                scans_table.drop(echo, inplace=True)
                            elif echo not in scans_table.index and (session/echo).is_file() and echo.split('/')[0] in bids.bidsdatatypes:
                                LOGGER.info(f"Adding '{echo}' to '{scans_tsv}'")
                                scans_table.loc[echo] = scans_table.loc['oldrow']       # NB: Assuming that the echo-rows are all identical

                        scans_table.drop('oldrow', inplace=True)
                        scans_table.sort_values(by=['acq_time','filename'], inplace=True)
                        scans_table.replace('','n/a').to_csv(scans_tsv, sep='\t', encoding='utf-8', na_rep='n/a')
                        for scan in scans_table.index:
                            if not (session/scan).is_file():
                                LOGGER.warning(f"Found non-existent file '{scan}' in '{scans_tsv}'")

    LOGGER.info('-------------- FINISHED! -------------')
    LOGGER.info('')


def main():
    """Console script usage"""

    class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter): pass

    parser = argparse.ArgumentParser(formatter_class=CustomFormatter,
                                     description=__doc__,
                                     epilog='examples:\n'
                                            '  echocombine /project/3017065.01/bids func/*task-stroop*echo-1*\n'
                                            '  echocombine /project/3017065.01/bids *task-stroop*echo-1* -p 001 003\n'
                                            '  echocombine /project/3017065.01/bids func/*task-*echo-1* -o func\n'
                                            '  echocombine /project/3017065.01/bids func/*task-*echo-1* -o derivatives -w 13 26 39 52\n'
                                            '  echocombine /project/3017065.01/bids func/*task-*echo-1* -a PAID\n ')
    parser.add_argument('bidsfolder', type=str,
                        help='The bids-directory with the (multi-echo) subject data')
    parser.add_argument('pattern', type=str,
                        help="Globlike recursive search pattern (relative to the subject/session folder) to select the first echo of the images that need to be combined, e.g. '*task-*echo-1*'")
    parser.add_argument('-p','--participant_label', type=str, nargs='+',
                        help='Space separated list of sub-# identifiers to be processed (the sub- prefix can be left out). If not specified then all sub-folders in the bidsfolder will be processed')
    parser.add_argument('-o','--output', type=str, default='',
                        help=f"A string that determines where the output is saved. It can be the name of a BIDS datatype folder, such as 'func', or of the derivatives folder, i.e. 'derivatives'. If output = [the name of the input datatype folder] then the original echo images are replaced by one combined image. If output is left empty then the combined image is saved in the input datatype folder and the original echo images are moved to the {unknowndatatype} folder")
    parser.add_argument('-a','--algorithm', choices=['PAID', 'TE', 'average'], default='TE',
                        help='Combination algorithm')
    parser.add_argument('-w','--weights', nargs='*', default=None, type=list,
                        help='Weights for each echo')
    parser.add_argument('-f','--force', action='store_true',
                        help='If this flag is given subjects will be processed, regardless of existing target files already exist. Otherwise the echo-combination will be skipped')
    args = parser.parse_args()

    echocombine(bidsdir   = args.bidsfolder,
                pattern   = args.pattern,
                subjects  = args.participant_label,
                output    = args.output,
                algorithm = args.algorithm,
                weights   = args.weights,
                force     = args.force)


if __name__ == '__main__':
    LOGGER = logging.getLogger(f"bidscoin.{Path(__file__).stem}")
    main()

else:
    LOGGER = logging.getLogger(__name__)
