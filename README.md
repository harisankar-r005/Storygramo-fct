# Storygramo template font replacement

Run the tool on one local category at a time. This repository contains the font audit, reviewed Anniversary matches, and the replacement fonts with their SIL Open Font License notices. Keep raw templates and generated ZIPs outside the repository.

## Run a category

Use Python 3.11 or later. From the cloned repository, run:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python replace_fonts.py "C:\templates\Anniversary" "C:\modified\Anniversary"
```

The first path is the unpacked category containing templates with a `template.json` file. The second must be a new folder outside that category. The tool writes one ZIP per flagged template after all packages pass integrity checks. For the audited Anniversary source, expect 26 ZIPs. Check HTML previews and test import and text editing in Storygramo before release.

## Prepare matches for another category

1. Check [the font audit](font_license_audit.csv) and the category's bundled fonts and animation references. The tool keeps fonts the audit marks open-license and requires a match for any other bundled font or missing font reference.
2. Compare the old and replacement fonts for weight, width, and lettering style. Add the replacement font and its official SIL Open Font License notice under `fonts/<family-directory>/`, then record the match and upstream URL in [font-matches.csv](font-matches.csv).
3. Use the old font filename or its animation alias in the **Original alias** column. An exact filename takes precedence when both rows exist. Leave **Weight** blank for a static font or specify the weight to use from a variable font.
4. Run the tool on the next category. It preserves resource filenames, rebuilds affected embedded text outlines in both animation files, and includes the replacement license notices. Missing matches stop the build so you can review the choice before generating packages.

Commit updates to the code, audit, match table, and licensed replacement font assets.
