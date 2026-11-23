#!/bin/bash
# Example batch script (Slurm) that splits the 88 configurations into 8 parts, one array task each.
# Replace every <...> before use; nothing here is specific to a cluster.  A run uses one core and up to 15 GB of memory,
# so --jobs times 15 GB should fit in the memory asked for.
#SBATCH --job-name=froq
#SBATCH --account=<ACCOUNT>
#SBATCH --partition=<PARTITION>
#SBATCH --nodes=1
#SBATCH --cpus-per-task=<CORES>
#SBATCH --mem=<MEMORY>
#SBATCH --time=<HH:MM:SS>
#SBATCH --array=1-8
set -euo pipefail
cd <PATH TO THIS REPOSITORY>
source <PATH TO THE VIRTUAL ENVIRONMENT>/bin/activate
python -m froq.configuration all --part "${SLURM_ARRAY_TASK_ID}/8" --jobs <RUNS AT ONCE> --out runs > "part_${SLURM_ARRAY_TASK_ID}.txt"
