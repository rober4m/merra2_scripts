#!/bin/bash
#SBATCH --job-name=meteo_data
#SBATCH -o stdout.%j
#SBATCH -e stderr.%j
#SBATCH --time=5-00:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4

# Path of the directory of the script
cd /path/to/script/

# Change NameofCondaEnv for the name of the conda enviroment with all the packages required. 
# This is the path of the conda env
$HOME/miniconda3/envs/{NameofCondaEnv}/bin/python merra2_parallel.py -l city_coordinates.csv -o merra2_output --workers 8
 