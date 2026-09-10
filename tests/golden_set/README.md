# Golden set

100 hand-labeled real syllabi (PRD section 16), stratified across:

- STEM lecture, humanities seminar, lab course, studio/performance, online async
- well-formatted modern PDF, LMS HTML export, scanned/photographed legacy doc, DOCX with tables
- short (1 page) and long (12+ pages)

Each label is a `labels/<name>.json` file shaped like the extraction schema
(section 8.2) plus a `source_paths` array pointing at the document it labels.
Treat this directory as data, DVC-tracked, not as code.

**These are not the fixtures in `tests/fixtures/syllabi/`.** Those are synthetic
documents that let the pipeline run on day one. The eval gate is only meaningful
against real syllabi from the target institution.

Run the gate:

```bash
pytest tests/golden_set -m eval
```

It skips while this directory is empty rather than reporting a vacuous pass.
