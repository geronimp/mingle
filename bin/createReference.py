#!/usr/bin/env python

import argparse
import logging
import sys
import os
import subprocess
import re
from Bio import SeqIO

from mingle.phil_format_database_parser import PhilFormatDatabaseParser
from mingle.graftm_stockholm_io import GraftMStockholmIterator
from mingle.taxonomy_string import TaxonomyString

# Inputs:
# greengenes file from genome tree database => provides taxonomy
# HMM => the HMM to search with
# path to proteome files - one for each genome => sequences to be put into the tree
default_evalue = '1e-20'
parser = argparse.ArgumentParser(description='''For a given HMM, create a phylogenetic tree and "Phil format" .greengenes file from the an ACE genome tree data (after the database info has been extracted)''')
parser.add_argument('--hmm', help = 'Hidden Markov model to search with', required = True)
parser.add_argument('--greengenes', help = '.greengenes file to define taxonomy', required = True)
parser.add_argument('--proteomes_list_file', help = 'path to file containing faa files of each genome\'s proteome (of the form [path]<ace_ID>[stuff].faa e.g. /srv/home/ben/A00000001.fna.faa', required = True)
parser.add_argument('--preparation_folder', help = 'store intermediate files in this folder [default: use temporary folder, delete after use]', default = None, required=True)

parser.add_argument('--evalue', help = 'evalue cutoff for the hmmsearch (default = %s)' % default_evalue, default = default_evalue)
parser.add_argument('--cpus', type=int, help = 'number of CPUs to use throughout the process', default = 1)

options = parser.parse_args()

# module load taxtastic hmmer fxtract fasttree
logging.basicConfig(level=logging.DEBUG)

# Given an iterable (e.g. a list) of taxonomies, write a new file containing a taxtastic-compatible
# taxonomy file to the given (presumed open) file handle. The ranks is a list of
# e.g. ['phylum','class']
def create_taxonomy_file(taxonomies, ranks, output):
  seen_taxonomies = {}# hash of full taxonomy to tax_id

  # Print headings
  headers = ["tax_id","parent_id","rank","tax_name","root"]
  headers.extend(ranks)
  output.write((",").join(headers))
  output.write("\n")
  num_cols = len(headers)

  # Print root
  roots = ['root','root',"root","root"]
  output.write((",").join(roots))
  for i in range(num_cols-len(roots)):
    output.write(',')
  output.write("\n")

  for tax_set in taxonomies:
    parent_id = 'root'
    tax_object = TaxonomyString(tax_set)
    splits = tax_object.names()

    if tax_object.full_name() == '':
      continue
    elif len(splits) > len(ranks):
      logging.warn("Ignoring taxon that appears malformed from the .greengenes file: %s" % tax_set)
    else:
      aboves = ['root']
      for rank_i, taxon in enumerate(splits):
        if rank_i == 0:
          current = 'root;'+taxon
        else:
          current = "%s;%s" % (current, taxon)

        aboves.append(current)

        # Already in tax file?
        if current in seen_taxonomies:
          # nothing to do, already seen this taxonomy
          pass
        else:
          seen_taxonomies[current] = current
          to_write = [current,str(parent_id),ranks[rank_i],taxon]
          to_write.extend(aboves)
          output.write((",").join(to_write))
          for i in range(num_cols-len(to_write)): #make it rectangular
            output.write(',')
          output.write("\n")

        # Set the parent ID whether this current has been seen previously or not
        parent_id = seen_taxonomies[current]


  return seen_taxonomies

# Return a hash of sequence name => sequence from the given stockholm
# file. Only return the sequence that is aligned to the HMM (not the lower
# case inserts)
def aligned_sequences_from_sto_file(sto_file):
  return GraftMStockholmIterator().readAlignedSequences(open(sto_file))



# Create temporary directory for everything to go in
dump_directory = options.preparation_folder
if os.path.exists(dump_directory):
  raise Exception("Prep folder %s already exists, cowardly refusing to proceed" % dump_directory)
os.mkdir(dump_directory)

# Read each of the proteome paths and strip() them
with open(options.proteomes_list_file) as f:
  proteomes = [pro.strip() for pro in f]
if len(proteomes) < 1: raise Exception("Error: no proteome files found, cannot continue")
logging.info("Read in %s proteome files for processing e.g. %s" % (len(proteomes), proteomes[0]))


# Read Phil format file, creating a hash of ACE ID to taxonomy
logging.info('Reading taxonomy information from .greengenes file..')
ace_id_to_taxonomy = {}
for entry in PhilFormatDatabaseParser().each(open(options.greengenes)):
  ace_id = entry['db_name']
  try:
    taxonomy = entry['genome_tree_tax_string']
    ace_id_to_taxonomy[ace_id] = TaxonomyString(taxonomy).full_name()
  except KeyError:
    logging.warn("taxonomy information not found in Phil format file for ID: %s, skipping" % ace_id)

logging.info("Read in taxonomy for %s genomes" % len(ace_id_to_taxonomy))


# create taxonomy file
logging.info('Creating taxonomy file..')
taxonomy_file_path = os.path.join(dump_directory, 'taxonomy.csv')
with open(taxonomy_file_path,'w') as taxonomy_fh:
  taxon_name_to_taxonomy_id = create_taxonomy_file(
    ace_id_to_taxonomy.values(),
    ('kingdom','phylum','class','order','family','genus','species'),
    taxonomy_fh
  )
logging.info('taxonomy file created.')


# Run hmmsearch on each of the proteomes
logging.info("Running hmmsearches..")
hmmsearches_directory = os.path.join(dump_directory, 'hmmsearches')
os.makedirs(hmmsearches_directory)
#TODO: the below fails if the given proteome list file is a pipe as opposed to a regular file. This script should read the list file,
# and then directly pass the list to parallel, and then all will be well.
command = "cat %s |parallel --gnu -j %s hmmsearch -E %s --domtblout %s/{/}.domtblout.csv %s {} '>' /dev/null" % (
  options.proteomes_list_file,
  options.cpus,
  options.evalue,
  hmmsearches_directory,
  options.hmm)
logging.info("Running cmd: %s" % command)
subprocess.check_call(["/bin/bash", "-c", command])


# Extract the proteins from each proteome that were hits
# hmmalign the hits back to the HMM
logging.info("Extracting hit proteins from proteome files and aligning hit proteins back to HMM..")
aligned_proteins_directory = os.path.join(dump_directory, 'hmmaligns')
os.makedirs(aligned_proteins_directory)
# hmmalign ../nifH.HMM <(fxtract -H -f <(grep -v ^\# ../hmmsearches/C00001470.fna.faa.domtblout.csv |awk '{print $1}' |sort |uniq) ../proteomes/C00001470.fna.faa) |seqmagick convert --input-format stockholm --output-format fasta - -
# grep -v ^\# C00001470.fna.faa.domtblout.csv |awk '{print $1}' |sort |uniq |fxtract -H -f /dev/stdin ../proteomes/C00001470.fna.faa
align_and_extract_script = os.path.join(os.path.dirname(os.path.realpath(__file__)),'alignAsNecessary.py')
# Usage: alignAsNecessary.py fasta_file hmm output_stockholm

command = "cat %s |parallel --gnu -j %s grep -v ^\# %s/{/}.domtblout.csv '|' cut -d'\" \"' -f1 '|' sort -u '|' %s {} %s %s/{/}.sto" % (
  options.proteomes_list_file,
  options.cpus,
  hmmsearches_directory,
  align_and_extract_script,
  options.hmm,
  aligned_proteins_directory,
  )
logging.info("Running cmd: %s" % command)
subprocess.check_call(["/bin/bash", "-c", command])

# Remove the unaligned parts of the stockholm file
logging.info("Concatenating and renaming files into a single aligned fasta file..")
# => use biopython's sto parser
# For each of the hmmaligned files, read in the
seqinfo_file = os.path.join(dump_directory, 'seq_info.csv')
sequence_number = 0
with open(seqinfo_file,'w') as seqinfo_fh:
  seqinfo_fh.write(",".join(["seqname","tax_id\n"])) #write header
  aligned_sequences_file = os.path.join(dump_directory, 'aligned.fasta')
  with open(aligned_sequences_file,'w') as aligned_sequences_fh:
    for proteome in proteomes:
      # Skip if the file does not exist - this indicates there was no hits
      sto_path = os.path.join(aligned_proteins_directory, "%s.sto" % os.path.basename(proteome))
      if not os.path.exists(sto_path): continue

      db_id = os.path.basename(proteome).split('.')[0]

      try:
        tax = ace_id_to_taxonomy[db_id]
        taxonomy_id = taxon_name_to_taxonomy_id['root;'+re.sub(' ','',tax)] #TODO: what happens if the ACE ID is not found?
      except KeyError:
        tax = None

      seqs = aligned_sequences_from_sto_file(sto_path)
      for name, seq in seqs.items():
        # Concatenate the aligned proteins into one file, renaming them so the ACE ID is at the front
        # Also add the sequence number, so that the names are garaunteed to be unique
        # (fasttree fails otherwise)
        new_name = "%s_%s" % (db_id, sequence_number+1)
        # Record any sequences found in the seqinfo file
        if tax is not None:
          seqinfo_fh.write(",".join((
            new_name,
            #new_name,
            str(taxonomy_id)+"\n",
            #'FALSE\n',
          )))
          aligned_sequences_fh.write(">%s\n" % new_name)
          aligned_sequences_fh.write("%s\n" % seq)
          sequence_number += 1
logging.info("Included %s sequences in the alignment" % (sequence_number))
if sequence_number == 0: raise Exception("No matching sequences detected, cannot create tree")
if sequence_number < 4: logging.warn("Too few sequences detected (%s)!!!!, the output is unlikely to be informative" % sequence_number)

# Make a tree, saving the log information
logging.info("Creating phylogenetic tree..")
fasttree_output = os.path.join(dump_directory, 'tree.nwk')
fasttree_log_file = os.path.join(dump_directory, 'fasttree.log')
subprocess.check_call(["bash","-c","FastTreeMP -log %s %s > %s" % (fasttree_log_file, aligned_sequences_file, fasttree_output)])

logging.info("Finished")