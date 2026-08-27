# generated-docs/

Output directory for the Repository Operations Agent. Analysis artefacts are
written here, **never** into `stage/` — repository analysis is read-only with
respect to the code it analyzes.

Layout, namespaced per staged repository so several repos can coexist:

```
generated-docs/
├── README.md                      this file (checked in)
├── diagrams/                      kept for tooling that expects a top-level dir
└── <reponame>/
    ├── OOP_ANALYSIS.md            counts, polymorphism, encapsulation, limitations
    ├── CLASS_CATALOG.md           every declared type, where it lives
    ├── INTERFACE_IMPLEMENTATIONS.md  implementation evidence + confidence
    ├── FUNCTION_CALL_GRAPH.md     repository-internal call sites
    └── diagrams/
        ├── class-diagram.mmd
        ├── inheritance-diagram.mmd
        ├── package-dependency.mmd
        └── component-diagram.mmd
```

Generated content is git-ignored. Regenerate it with:

```bash
make generate-diagrams REPO=<path-or-staged-repo-name>
# or
curl -X POST localhost:8000/api/repos/<reponame>/diagrams \
     -H 'content-type: application/json' \
     -d '{"kinds":["class","inheritance","dependency"],"write":true,"write_documents":true}'
```

Override the root with `ZDOCS_GENERATED_DOCS_DIR`.
