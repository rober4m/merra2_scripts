#!/bin/bash
#SBATCH --job-name=merra2_parallel
#SBATCH -o stdout.%j
#SBATCH -e stderr.%j
#SBATCH --time=2-00:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4

# Change NameofCondaEnv for the name of the conda enviroment with all the packages required. 
# This is the path of the conda env
$HOME/miniconda3/envs/{NameofCondaEnv}/bin/python merra2_parallel.py --locations city_coordinates.csv -o merra2_output
 