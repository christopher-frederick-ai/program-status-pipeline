# files/

The shared runtime folder. Docker mounts it into the containers as `/files`, so n8n and the deck service both
read and write here: the weekly `report_<date>.json` and `.txt`, `milestones.xlsx`, and the generated decks
under `decks/`. Everything in it except this file is git-ignored.

To run the pipeline on the invented sample program, copy the source files in once:

    Copy-Item sample_data\* files\        # Windows PowerShell
    cp sample_data/* files/               # macOS or Linux

`sample_data/` is the single committed copy of the source data (it also feeds the tests and scripts). Use your
own milestone sheet and exports here when you adapt the pipeline.
