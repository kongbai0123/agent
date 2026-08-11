# Third-party frontend assets

The Local AI Workbench serves these pinned assets from its own loopback origin.
Their upstream license texts are retained under `vendor/licenses/`.

| Package | Version | Upstream | License file |
| --- | ---: | --- | --- |
| Lucide | 1.27.0 | https://www.npmjs.com/package/lucide | `licenses/lucide-LICENSE.txt` |
| marked | 12.0.2 | https://www.npmjs.com/package/marked | `licenses/marked-LICENSE.md` |
| DOMPurify | 3.1.5 | https://www.npmjs.com/package/dompurify | `licenses/dompurify-LICENSE.txt` |

These files are intentionally versioned rather than fetched from a CDN at
runtime. Updating one requires updating its filename, this notice, and the
corresponding references in `frontend/index.html`.
