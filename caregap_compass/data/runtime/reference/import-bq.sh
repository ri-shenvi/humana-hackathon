#!/bin/bash
# import_to_bigquery.sh
#
# This script automates the creation of a BigQuery dataset called `humana_hackathon`
# and imports all CSV files from a specified directory into BigQuery tables
# using the `bq` command-line tool (which is pre-installed in Google Cloud Shell).
#
# Usage in Google Cloud Shell:
# 1. Upload this script and the CSV files to Cloud Shell.
# 2. Make the script executable:
#    chmod +x import_to_bigquery.sh
# 3. Run the script:
#    ./import_to_bigquery.sh /path/to/csv/folder
#    (Or just run `./import_to_bigquery.sh` if the CSV files are in the same folder)
# Exit on any error
set -e
# Set directory to first argument, or default to current directory
CSV_DIR="${1:-.}"
# Dataset name
DATASET_ID="humana_hackathon"
LOCATION="US"
# Check if directory exists
if [ ! -d "$CSV_DIR" ]; then
    echo "Error: Directory '$CSV_DIR' does not exist."
    exit 1
fi
echo "Using CSV Directory: $CSV_DIR"
# Check if dataset exists. If not, create it.
if bq show "$DATASET_ID" > /dev/null 2>&1; then
    echo "Dataset '$DATASET_ID' already exists."
else
    echo "Creating dataset '$DATASET_ID' in location '$LOCATION'..."
    bq mk --location="$LOCATION" --dataset "$DATASET_ID"
    echo "Dataset created successfully."
fi
# Find and loop through all CSV files in the directory
# Using a glob that handles spaces if they exist (though these files are snake_case)
csv_count=0
for file in "$CSV_DIR"/*.csv; do
    # Ensure it's a file and not a glob that didn't match anything
    if [ -f "$file" ]; then
        filename=$(basename -- "$file")
        table_name="${filename%.csv}"
        
        echo ""
        echo "--------------------------------------------------"
        echo "Loading $filename into $DATASET_ID.$table_name..."
        echo "--------------------------------------------------"
        
        # Load CSV using schema autodetect
        # --replace is equivalent to WRITE_TRUNCATE (overwrites table if it exists)
        bq load \
            --source_format=CSV \
            --autodetect \
            --skip_leading_rows=1 \
            --replace \
            "$DATASET_ID.$table_name" \
            "$file"
            
        csv_count=$((csv_count + 1))
    fi
done
if [ "$csv_count" -eq 0 ]; then
    echo "No CSV files found in '$CSV_DIR'."
else
    echo ""
    echo "Successfully loaded $csv_count CSV file(s) into BigQuery dataset '$DATASET_ID'."
fi
