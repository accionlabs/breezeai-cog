"""TerraformParser — Terraform-specific HCL parser.

Selected over the base :class:`~..hcl.parser.HclParser` only for genuine Terraform files:
- ``.tf`` / ``.tfvars`` are Terraform-exclusive extensions → always claimed.
- ``.hcl`` is shared with Packer, Nomad, Vault, Terragrunt, etc. → claimed only when
  the file contains at least one Terraform-signature block keyword (``resource``,
  ``provider``, ``terraform``, ``module``).
"""

from __future__ import annotations

import re

from ..hcl.parser import HclParser

# Matches a top-level HCL block whose keyword is a Terraform primitive.
# Anchored at the start of a line so embedded strings don't trigger a false positive.
_TF_BLOCK_RE = re.compile(
    rb'(?m)^\s*(?:resource|provider|terraform|module)\s+["{]',
)


class TerraformParser(HclParser):
    name = "hcl-terraform"
    priority = 10
    _framework = "terraform"

    def claims(self, path: str, source: bytes) -> bool:
        from pathlib import Path

        suffix = Path(path).suffix
        if suffix in (".tf", ".tfvars"):
            return True
        if suffix == ".hcl":
            return bool(_TF_BLOCK_RE.search(source))
        return False
