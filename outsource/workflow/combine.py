#!/usr/bin/env python
import argparse
import os
import shutil
import numpy as np
import strax
import straxen
straxen.Events.save_when = strax.SaveWhen.TARGET
print("We have forced events to save always.")
import datetime
import time
import admix
import rucio
from utilix import DB, uconfig
from immutabledict import immutabledict
import cutax
import glob
import json

from admix.clients import rucio_client

admix.clients._init_clients()

def get_hashes(st):
    return {dt: item['hash'] for dt, item in st.provided_dtypes().items()}

def merge(runid_str, # run number padded with 0s
          dtype,     # data type 'level' e.g. records, peaklets
          st,        # strax context
          path       # path where the data is stored
          ):

    # get the storage path, since will need to reset later
    _storage_paths = [storage.path for storage in st.storage]

    # initialize plugin needed for processing
    plugin = st._get_plugins((dtype,), runid_str)[dtype]
    st._set_plugin_config(plugin, runid_str, tolerant=False)
    plugin.setup()

    to_merge = [d.split('-')[1] for d in os.listdir(path)]

    for keystring in plugin.provides:
        if keystring not in to_merge:
            continue
        key = strax.DataKey(runid_str, keystring, plugin.lineage)
        saver = st.storage[0].saver(key, plugin.metadata(runid_str, keystring))
        # monkey patch the saver
        tmpname = os.path.split(saver.tempdirname)[1]
        dirname = os.path.split(saver.dirname)[1]
        saver.tempdirname = os.path.join(path, tmpname)
        saver.dirname = os.path.join(path, dirname)
        saver.is_forked = True
        # merge the jsons
        saver.close()

    # change the storage frontend to use the merged data
    st.storage[0] = strax.DataDirectory(path)

    # rechunk the data if we can
    for keystring in plugin.provides:
        if keystring not in to_merge:
            continue
        rechunk = True
        if isinstance(plugin.rechunk_on_save, immutabledict):
            if not plugin.rechunk_on_save[keystring]:
                rechunk = False
        else:
            if not plugin.rechunk_on_save:
                rechunk = False

        if rechunk:
            print(f"Rechunking {keystring}")
            st.copy_to_frontend(runid_str, keystring, 1, rechunk=True)
        else:
            print(f"Not rechunking {keystring}. Just copy to the staging directory.")
            key = st.key_for(runid_str, keystring)
            src = os.path.join(st.storage[0].path, str(key))
            dest = os.path.join(st.storage[1].path, str(key))
            shutil.copytree(src, dest)

    # reset in case we need to merge more data
    st.storage = [strax.DataDirectory(path) for path in _storage_paths]

def check_chunk_n(directory):
    """
    Check that the chunk length and number of chunk is agreed with promise in metadata.
    """
    if directory[-1] != '/':
        directory += '/'
    files = sorted(glob.glob(directory+'*'))
    n_chunks = len(files) - 1
    metadata = json.loads(open(files[-1], 'r').read())

    if n_chunks != 0:
        n_metadata_chunks = len(metadata['chunks'])
        # check that the number of chunks in storage is less than or equal to the number of chunks in metadata
        assert n_chunks == n_metadata_chunks or n_chunks == n_metadata_chunks-1, "For directory %s, \
                                               there are %s chunks in storage, \
                                               but metadata says %s. Chunks in storage must be \
                                               less than chunks in metadata!"%(
                                                        directory, n_chunks, n_metadata_chunks)
        
        compressor = metadata['compressor']
        dtype = eval(metadata['dtype'])
        
        # check that the chunk length is agreed with promise in metadata
        for i in range(n_chunks):
            chunk = strax.load_file(files[i], compressor=compressor, dtype=dtype)
            if metadata['chunks'][i]['n'] != len(chunk):
                raise strax.DataCorrupted(
                    f"Chunk {files[i]} of {metadata['run_id']} has {len(chunk)} items, "
                    f"but metadata says {metadata['chunks'][i]['n']}")

        # check that the last chunk is empty
        if n_chunks == n_metadata_chunks-1:
            assert metadata['chunks'][n_chunks]['n'] == 0, "Empty chunk has non-zero length in metadata!"

    else:
        # check that the number of chunks in metadata is 1
        assert len(metadata['chunks']) == 1, "There are %s chunks in storage, but metadata says %s"%(n_chunks, len(metadata['chunks']))
        assert metadata['chunks'][0]['n'] == 0, "Empty chunk has non-zero length in metadata!"
    

def main():
    parser = argparse.ArgumentParser(description="Combine strax output")
    parser.add_argument('dataset', help='Run number', type=int)
    parser.add_argument('dtype', help='dtype to combine')
    parser.add_argument('--context', help='Strax context')
    parser.add_argument('--input', help='path where the temp directory is')
    parser.add_argument('--update-db', help='flag to update runsDB', dest='update_db',
                        action='store_true')
    parser.add_argument('--upload-to-rucio', help='flag to upload to rucio', dest='upload_to_rucio',
                        action='store_true')

    args = parser.parse_args()

    runid = args.dataset
    runid_str = "%06d" % runid
    path = args.input

    final_path = 'finished_data'

    # get context
    st = getattr(cutax.contexts, args.context)()
    st.storage = [strax.DataDirectory('./'),
                  strax.DataDirectory(final_path) # where we are copying data to
                  ]

    # check what data is in the output folder
    dtypes = [d.split('-')[1] for d in os.listdir(path)]

    if any([d in dtypes for d in ['lone_hits', 'pulse_counts', 'veto_regions']]):
        plugin_levels = ['records', 'peaklets']
    elif 'hitlets_nv' in dtypes:
        plugin_levels = ['hitlets_nv']
    elif 'afterpulses' in dtypes:
        plugin_levels = ['afterpulses']
    elif 'led_calibration' in dtypes:
        plugin_levels = ['led_calibration']
    else:
        plugin_levels = ['peaklets']

    # merge
    for dtype in plugin_levels:
        print(f"Merging {dtype} level")
        merge(runid_str, dtype, st, path)

    #print(f"Current contents of {final_path}:")
    #print(os.listdir(final_path))

    # now upload the merged metadata
    # setup the rucio client(s)
    if not args.upload_to_rucio:
        print("Ignoring rucio upload. Exiting")
        return

    # need to patch the storage one last time
    st.storage = [strax.DataDirectory(final_path)]

    for this_dir in os.listdir(final_path):
        # prepare list of dicts to be uploaded
        _run, keystring, straxhash = this_dir.split('-')

        # We don't want to upload records to rucio
        if keystring == 'records' or keystring == 'records_nv':
            print("We don't want to upload %s to rucio. Skipping."%(keystring))
            continue

        dataset_did = admix.utils.make_did(runid, keystring, straxhash)
        scope, dset_name = dataset_did.split(':')

        # based on the dtype and the utilix config, where should this data go?
        if keystring in ['records', 'pulse_counts', 'veto_regions']:
            rse = uconfig.get('Outsource', 'records_rse')
        elif keystring in ['peaklets', 'lone_hits', 'merged_s2s', 'hitlets_nv']:
            rse = uconfig.get('Outsource', 'peaklets_rse')
        else:
            rse = uconfig.get('Outsource', 'events_rse')

        # Test if the data is complete
        print("--------------------------")
        try:
            print("Try loading data in %s to see if it is complete."%(this_dir))
            st.get_array(runid_str, keystring, keep_columns='time', progress_bar=False)
            print("Successfully loaded %s! It is complete."%(this_dir))
        except Exception as e:
            print(f"Data is not complete for {this_dir}. Skipping")
            print("Below is the error message we get when trying to load the data:")
            print(e)

        this_path = os.path.join(final_path, this_dir)
        contents_to_upload = os.listdir(this_path)

        print("--------------------------")
        print(f"Checking if chunk length is agreed with promise in metadata for {this_dir}")
        check_chunk_n(this_path)
        print("The chunk length is agreed with promise in metadata.")

        print("--------------------------")
        print(f"Trying to upload {this_path} to {rse}")

        if len(contents_to_upload):
            print(f"Pre-uploading {path} to rucio!")
            t0 = time.time()
            admix.preupload(path, rse=rse, did=dataset_did)
            preupload_time = time.time() - t0
            print(f"=== Preuploading time for {keystring}: {preupload_time/60:0.2f} minutes === ")
            print("--------------------------")

            print("Here are the contents to upload:")
            print(contents_to_upload)
            t0 = time.time()
            admix.upload(this_path, rse=rse, did=dataset_did, update_db=args.update_db)
            print(f"Uploaded {this_path} to {rse} with did {dataset_did}. ")
            upload_time = time.time() - t0
            print(f"=== Uploading time for {keystring}: {upload_time/60:0.2f} minutes === ")
        else:
            raise ValueError("Failed admix upload! The following files are inside %s: %s"%(
                this_path, contents_to_upload))
        

if __name__ == "__main__":
    main()
