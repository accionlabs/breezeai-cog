"""TerraformParser: statement emission (gated by capture_statements), module Class records,
externalImports, platform detection, endpoint addressing, ignore patterns, and registry integration."""

from __future__ import annotations

import json

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.hcl.parser import HclParser
from breezeai_cog.parsers.hcl_terraform.parser import TerraformParser

# ── fixtures ──────────────────────────────────────────────────────────────────

_TF_SRC = b"""\
resource "aws_s3_bucket" "my_bucket" {
  bucket = "my-bucket-name"
  tags = {
    Environment = "production"
  }
}

resource "aws_instance" "web" {
  ami           = "ami-12345678"
  instance_type = "t3.micro"
}

data "aws_ami" "ubuntu" {
  most_recent = true
}

variable "region" {
  type    = string
  default = "us-east-1"
}

output "bucket_arn" {
  value = aws_s3_bucket.my_bucket.arn
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "3.0.0"
  cidr    = "10.0.0.0/16"
}

module "local_mod" {
  source = "./modules/shared"
}

provider "aws" {
  region = "us-east-1"
}

locals {
  env = "production"
}

terraform {
  required_version = ">= 1.0"
}
"""

_TFVARS_SRC = b"""\
region         = "eu-west-1"
instance_count = 5
enable_logging = true
"""

_AZURE_SRC = b"""\
resource "azurerm_resource_group" "rg" {
  name     = "my-rg"
  location = "East US"
}

resource "azurerm_storage_account" "store" {
  name                     = "mystorageacct"
  resource_group_name      = azurerm_resource_group.rg.name
  account_tier             = "Standard"
  account_replication_type = "LRS"
}
"""

_GCP_SRC = b"""\
resource "google_storage_bucket" "assets" {
  name     = "my-bucket"
  location = "US"
}

provider "google" {
  project = "my-project"
}
"""

_PROVIDERS_SRC = b"""\
terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 4.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}
"""

_DEDUP_SRC = b"""\
module "vpc_a" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "3.0.0"
}

module "vpc_b" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "4.0.0"
}
"""

_VAR_SOURCE_SRC = b"""\
module "custom" {
  source = var.module_source
}
"""

_GENERIC_SRC = b"""\
resource "random_id" "suffix" {
  byte_length = 8
}

resource "local_file" "config" {
  content  = "hello"
  filename = "/tmp/config.txt"
}
"""

_MIXED_SRC = b"""\
resource "aws_s3_bucket" "bucket" {
  bucket = "my-bucket"
}

resource "azurerm_resource_group" "rg" {
  name     = "my-rg"
  location = "East US"
}
"""

_PROVIDER_ONLY_SRC = b"""\
provider "aws" {
  region = "us-east-1"
}
"""

_MODULE_ATTRS_SRC = b"""\
module "network" {
  source      = "terraform-aws-modules/vpc/aws"
  version     = "3.0.0"
  cidr        = "10.0.0.0/16"
  enable_nat  = true
  az_count    = 3
}
"""


def _parse(path: str, source: bytes, *, capture_statements: bool = False):
    ctx = ParseContext(
        path=path,
        abs_path=None,
        source=source,
        repo_root=".",
        capture_statements=capture_statements,
    )
    return TerraformParser().parse_file(ctx)


def test_local_module_source_resolves_to_import_files(tmp_path) -> None:
    # A local `module` source names a directory; it must resolve to the .tf files it loads
    # (File→File IMPORTS), while a registry source stays an externalImport.
    (tmp_path / "terraform/modules/app-network").mkdir(parents=True)
    (tmp_path / "terraform/modules/app-network/main.tf").write_text('resource "aws_lb" "x" {}\n')
    (tmp_path / "terraform/modules/app-network/variables.tf").write_text('variable "env" {}\n')
    (tmp_path / "terraform/env/prod").mkdir(parents=True)
    env_tf = tmp_path / "terraform/env/prod/api.tf"
    env_tf.write_text(
        'module "ecs" {\n  source = "../../modules/app-network"\n}\n'
        'module "vpc" {\n  source = "terraform-aws-modules/vpc/aws"\n}\n'
    )
    parser = TerraformParser()
    idx = parser.build_index(tmp_path, list(tmp_path.rglob("*.tf")))
    ctx = ParseContext(path="terraform/env/prod/api.tf", abs_path=env_tf,
                       source=env_tf.read_bytes(), repo_root=tmp_path, resolution_index=idx)
    rec = parser.parse_file(ctx)
    assert rec.importFiles == [
        "terraform/modules/app-network/main.tf",
        "terraform/modules/app-network/variables.tf",
    ]
    assert rec.externalImports == ["terraform-aws-modules/vpc/aws"]  # registry source unchanged


def test_local_module_source_unresolved_is_honest_null(tmp_path) -> None:
    # A local source whose target dir isn't indexed yields no edge (honest-null), never a
    # dangling importFiles entry.
    env_tf = tmp_path / "main.tf"
    env_tf.write_text('module "m" {\n  source = "./nope"\n}\n')
    parser = TerraformParser()
    idx = parser.build_index(tmp_path, list(tmp_path.rglob("*.tf")))
    ctx = ParseContext(path="main.tf", abs_path=env_tf, source=env_tf.read_bytes(),
                       repo_root=tmp_path, resolution_index=idx)
    rec = parser.parse_file(ctx)
    assert rec.importFiles == []
    assert rec.externalImports == []


# ── FileRecord shape ──────────────────────────────────────────────────────────


def test_emits_config_record() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert rec.type == "config"
    assert rec.language == "hcl"
    assert rec.framework == "terraform"


def test_metadata_category_is_iac() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert rec.metadata == {"category": "iac"}


def test_no_functions() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert rec.functions == []


def test_loc_positive() -> None:
    assert _parse("main.tf", _TF_SRC).loc > 0


# ── extensions ───────────────────────────────────────────────────────────────


def test_hcl_extension_handled() -> None:
    rec = _parse("backend.hcl", _TF_SRC)
    assert rec.language == "hcl"
    assert rec.framework == "terraform"


def test_tfvars_extension_handled() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC)
    assert rec.language == "hcl"


# ── statements gating ─────────────────────────────────────────────────────────


def test_no_statements_without_flag() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=False)
    assert rec.statements == []


def test_statements_emitted_with_flag() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert len(rec.statements) > 0


# ── statement nodeType ────────────────────────────────────────────────────────


def test_tf_block_node_type_is_block() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert all(s.nodeType == "block" for s in rec.statements)


def test_tfvars_node_type_is_attribute() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    assert all(s.nodeType == "attribute" for s in rec.statements)


# ── statement semanticType ────────────────────────────────────────────────────


def test_resource_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    resource_stmts = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert len(resource_stmts) == 2


def test_data_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_data" for s in rec.statements)


def test_module_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_module" for s in rec.statements)


def test_tfvars_semantic_type_is_variable_value() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    assert all(s.semanticType == "iac_variable_value" for s in rec.statements)


def test_variable_block_has_no_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    var_stmt = next(s for s in rec.statements if s.name == "region" and s.nodeType == "block")
    assert var_stmt.semanticType is None


def test_output_block_has_no_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    out_stmt = next(s for s in rec.statements if s.name == "bucket_arn")
    assert out_stmt.semanticType is None


def test_provider_block_has_no_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    prov = next(s for s in rec.statements if s.name == "aws" and s.nodeType == "block"
                and "provider" in s.text)
    assert prov.semanticType is None


def test_locals_block_has_no_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    loc_stmt = next(s for s in rec.statements if s.name is None and "locals" in s.text)
    assert loc_stmt.semanticType is None


def test_terraform_block_has_no_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    tf_stmt = next(s for s in rec.statements if s.name is None and "required_version" in s.text)
    assert tf_stmt.semanticType is None


# ── statement name ────────────────────────────────────────────────────────────


def test_resource_statement_name_is_resource_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    names = {s.name for s in rec.statements if s.semanticType == "iac_resource"}
    assert "aws_s3_bucket" in names
    assert "aws_instance" in names


def test_data_statement_name_is_data_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data_stmt = next(s for s in rec.statements if s.semanticType == "iac_data")
    assert data_stmt.name == "aws_ami"


def test_variable_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    var_stmt = next(s for s in rec.statements if s.name == "region" and s.nodeType == "block")
    assert var_stmt.name == "region"


def test_output_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    out_stmt = next(s for s in rec.statements if s.name == "bucket_arn")
    assert out_stmt.name == "bucket_arn"


def test_module_statement_name_is_instance_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    mod_stmts = {s.name for s in rec.statements if s.semanticType == "iac_module"}
    assert "vpc" in mod_stmts
    assert "local_mod" in mod_stmts


def test_provider_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    prov = next(s for s in rec.statements if "provider" in s.text and s.name == "aws")
    assert prov.name == "aws"


def test_locals_statement_has_no_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    loc_stmt = next(s for s in rec.statements if "locals" in s.text and s.name is None)
    assert loc_stmt.name is None


# ── statement endpoint (Terraform address) ────────────────────────────────────


def test_resource_endpoint_is_terraform_address() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    endpoints = {s.endpoint for s in rec.statements if s.semanticType == "iac_resource"}
    assert "aws_s3_bucket.my_bucket" in endpoints
    assert "aws_instance.web" in endpoints


def test_data_endpoint_has_data_prefix() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data_stmt = next(s for s in rec.statements if s.semanticType == "iac_data")
    assert data_stmt.endpoint == "data.aws_ami.ubuntu"


def test_module_endpoint_has_module_prefix() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    vpc_stmt = next(s for s in rec.statements if s.semanticType == "iac_module" and s.name == "vpc")
    assert vpc_stmt.endpoint == "module.vpc"


def test_structure_only_blocks_have_no_endpoint() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    no_endpoint = [s for s in rec.statements if s.semanticType is None]
    assert all(s.endpoint is None for s in no_endpoint)


# ── statement line numbers / ids ──────────────────────────────────────────────


def test_statement_line_numbers() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    first = rec.statements[0]
    assert isinstance(first.startLine, int) and first.startLine >= 1
    assert first.endLine >= first.startLine


def test_statement_parent_id_is_file_id() -> None:
    rec = _parse("infra/main.tf", _TF_SRC, capture_statements=True)
    assert all(s.parentId == rec.id for s in rec.statements)


def test_statement_ids_unique() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    ids = [s.id for s in rec.statements]
    assert len(ids) == len(set(ids))


# ── statement text (the key field for MCP containsi queries) ──────────────────


def test_statement_text_contains_block_source() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    bucket = next(s for s in rec.statements if s.endpoint == "aws_s3_bucket.my_bucket")
    assert "aws_s3_bucket" in bucket.text
    assert "my_bucket" in bucket.text
    assert "my-bucket-name" in bucket.text


def test_containsi_azure_matches_azure_resources() -> None:
    rec = _parse("azure.tf", _AZURE_SRC, capture_statements=True)
    matching = [s for s in rec.statements if "azure" in s.text.lower()]
    assert len(matching) == 2


def test_containsi_azure_no_match_on_aws_file() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    matching = [s for s in rec.statements if "azure" in s.text.lower()]
    assert matching == []


# ── platform detection ────────────────────────────────────────────────────────


def test_aws_resource_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform == "aws" for s in resources)


def test_aws_data_source_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data_stmts = [s for s in rec.statements if s.semanticType == "iac_data"]
    assert all(s.platform == "aws" for s in data_stmts)


def test_azure_resource_platform() -> None:
    rec = _parse("azure.tf", _AZURE_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform == "azure" for s in resources)


def test_gcp_resource_platform() -> None:
    rec = _parse("gcp.tf", _GCP_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform == "gcp" for s in resources)


def test_generic_resource_no_platform() -> None:
    rec = _parse("generic.tf", _GENERIC_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform is None for s in resources)


def test_structure_only_blocks_no_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    no_semantic = [s for s in rec.statements if s.semanticType is None]
    assert all(s.platform is None for s in no_semantic)


def test_file_platform_aws() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert rec.platform == "aws"


def test_file_platform_azure() -> None:
    rec = _parse("azure.tf", _AZURE_SRC, capture_statements=True)
    assert rec.platform == "azure"


def test_file_platform_gcp() -> None:
    rec = _parse("gcp.tf", _GCP_SRC, capture_statements=True)
    assert rec.platform == "gcp"


def test_file_platform_none_for_generic() -> None:
    rec = _parse("generic.tf", _GENERIC_SRC, capture_statements=True)
    assert rec.platform is None


def test_file_platform_from_provider_even_without_capture_statements() -> None:
    # provider block is always scanned (ungated) — same as externalImports collection —
    # so file-level platform is available without --capture-statements
    rec = _parse("main.tf", _TF_SRC, capture_statements=False)
    assert rec.platform == "aws"


def test_file_platform_null_on_mixed_providers() -> None:
    # mixed aws + azure in same file → no unanimous agreement → null
    rec = _parse("mixed.tf", _MIXED_SRC, capture_statements=True)
    assert rec.platform is None


def test_file_platform_from_provider_block_fallback() -> None:
    # provider-only file with no resource blocks → platform from provider block
    rec = _parse("provider.tf", _PROVIDER_ONLY_SRC, capture_statements=True)
    assert rec.platform == "aws"


def test_provider_block_platform_is_null_at_statement_level() -> None:
    # provider is structure-only: its Statement has no platform
    rec = _parse("provider.tf", _PROVIDER_ONLY_SRC, capture_statements=True)
    prov_stmt = next(s for s in rec.statements if "provider" in s.text)
    assert prov_stmt.platform is None


# ── module Class records ──────────────────────────────────────────────────────


def test_module_yields_class_record() -> None:
    rec = _parse("main.tf", _TF_SRC)
    class_names = {c.name for c in rec.classes}
    assert "vpc" in class_names
    assert "local_mod" in class_names


def test_module_class_type_is_module() -> None:
    rec = _parse("main.tf", _TF_SRC)
    for cls in rec.classes:
        assert cls.type == "module"


def test_module_class_parent_id_is_file_id() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert all(c.parentId == rec.id for c in rec.classes)


def test_module_class_ids_unique() -> None:
    rec = _parse("main.tf", _DEDUP_SRC)
    ids = [c.id for c in rec.classes]
    assert len(ids) == len(set(ids))


def test_module_class_emitted_without_capture_statements() -> None:
    # Class records are not gated by --capture-statements
    rec = _parse("main.tf", _TF_SRC, capture_statements=False)
    assert len(rec.classes) > 0


def test_module_constructor_params_exclude_meta_args() -> None:
    rec = _parse("network.tf", _MODULE_ATTRS_SRC)
    net_cls = next(c for c in rec.classes if c.name == "network")
    param_names = {p.name for p in net_cls.constructorParams}
    # meta-args must be excluded
    assert "source" not in param_names
    assert "version" not in param_names
    # input variables must be included
    assert "cidr" in param_names
    assert "enable_nat" in param_names
    assert "az_count" in param_names


def test_module_constructor_params_type_inference() -> None:
    rec = _parse("network.tf", _MODULE_ATTRS_SRC)
    net_cls = next(c for c in rec.classes if c.name == "network")
    params = {p.name: p.type for p in net_cls.constructorParams}
    assert params["cidr"] == "string"
    assert params["enable_nat"] == "bool"
    assert params["az_count"] == "number"


def test_module_class_line_numbers() -> None:
    rec = _parse("network.tf", _MODULE_ATTRS_SRC)
    net_cls = next(c for c in rec.classes if c.name == "network")
    assert net_cls.startLine >= 1
    assert net_cls.endLine >= net_cls.startLine


# ── .tfvars statements ────────────────────────────────────────────────────────


def test_tfvars_emits_variable_value_statements() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    assert rec.type == "config"
    assert rec.language == "hcl"
    assert rec.framework == "terraform"
    node_types = {s.nodeType for s in rec.statements}
    assert node_types == {"attribute"}


def test_tfvars_statement_names_are_variable_names() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    names = {s.name for s in rec.statements}
    assert names == {"region", "instance_count", "enable_logging"}


def test_tfvars_statement_text_contains_value() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    region_stmt = next(s for s in rec.statements if s.name == "region")
    assert "eu-west-1" in region_stmt.text


def test_tfvars_no_statements_without_flag() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=False)
    assert rec.statements == []


def test_tfvars_no_classes() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC)
    assert rec.classes == []


# ── externalImports: module sources (ungated) ─────────────────────────────────


def test_registry_module_in_external_imports() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert "terraform-aws-modules/vpc/aws" in rec.externalImports


def test_local_module_not_in_external_imports() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert not any(s.startswith("./") for s in rec.externalImports)


def test_external_imports_populated_without_capture_statements() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=False)
    assert "terraform-aws-modules/vpc/aws" in rec.externalImports


def test_tfvars_has_no_external_imports() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC)
    assert rec.externalImports == []


# ── externalImports: required_providers sources ───────────────────────────────


def test_required_providers_sources_in_external_imports() -> None:
    rec = _parse("main.tf", _PROVIDERS_SRC)
    assert "hashicorp/aws" in rec.externalImports
    assert "hashicorp/azurerm" in rec.externalImports


def test_required_providers_without_capture_statements() -> None:
    rec = _parse("main.tf", _PROVIDERS_SRC, capture_statements=False)
    assert "hashicorp/aws" in rec.externalImports


# ── externalImports: deduplication ───────────────────────────────────────────


def test_duplicate_module_sources_deduped() -> None:
    rec = _parse("main.tf", _DEDUP_SRC)
    assert rec.externalImports.count("terraform-aws-modules/vpc/aws") == 1


# ── externalImports: variable-reference module source fallback ────────────────


def test_module_variable_source_captured() -> None:
    rec = _parse("main.tf", _VAR_SOURCE_SRC)
    assert len(rec.externalImports) == 1
    assert "module_source" in rec.externalImports[0]


# ── empty file ────────────────────────────────────────────────────────────────


def test_empty_tf_file() -> None:
    rec = _parse("empty.tf", b"", capture_statements=True)
    assert rec.type == "config"
    assert rec.statements == []
    assert rec.externalImports == []
    assert rec.classes == []


# ── ignore patterns ───────────────────────────────────────────────────────────


def test_ignore_patterns_include_terraform_dir() -> None:
    assert any(".terraform/" in p for p in TerraformParser().ignore_patterns())
    assert any(".terraform/" in p for p in HclParser().ignore_patterns())


def test_ignore_patterns_include_override() -> None:
    assert any("override.tf" in p for p in TerraformParser().ignore_patterns())
    assert any("override.tf" in p for p in HclParser().ignore_patterns())


def test_ignore_patterns_no_dead_entries() -> None:
    patterns = TerraformParser().ignore_patterns()
    assert not any("tfstate" in p for p in patterns)
    assert not any("lock.hcl" in p for p in patterns)


# ── schema validity ───────────────────────────────────────────────────────────


def test_record_serializes_schema_valid() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data = json.loads(rec.model_dump_json(by_alias=True, exclude_none=True))
    assert data["type"] == "config"
    assert data["language"] == "hcl"
    assert data["framework"] == "terraform"
    assert data["platform"] == "aws"
    assert "statements" in data
    assert data["metadata"] == {"category": "iac"}


def test_tfvars_record_serializes_schema_valid() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    data = json.loads(rec.model_dump_json(by_alias=True, exclude_none=True))
    assert data["type"] == "config"
    assert data["language"] == "hcl"
    assert data["framework"] == "terraform"
    assert "statements" in data


def test_platform_absent_from_json_when_none() -> None:
    rec = _parse("generic.tf", _GENERIC_SRC, capture_statements=True)
    data = json.loads(rec.model_dump_json(by_alias=True, exclude_none=True))
    assert "platform" not in data


def test_endpoint_present_for_resource_in_json() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data = json.loads(rec.model_dump_json(by_alias=True, exclude_none=True))
    resource_stmts = [s for s in data["statements"] if s.get("semanticType") == "iac_resource"]
    assert all("endpoint" in s for s in resource_stmts)


def test_endpoint_absent_from_json_for_structure_blocks() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data = json.loads(rec.model_dump_json(by_alias=True, exclude_none=True))
    no_semantic = [s for s in data["statements"] if "semanticType" not in s]
    assert all("endpoint" not in s for s in no_semantic)


# ── registry integration ──────────────────────────────────────────────────────


def test_registry_selects_hcl_terraform_for_tf() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        assert registry.select("infra/main.tf", b"").name == "hcl-terraform"
    finally:
        registry.clear()
        registry.discover_builtin()


def test_registry_selects_hcl_terraform_for_tfvars() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        assert registry.select("prod.tfvars", b"region = \"us-east-1\"").name == "hcl-terraform"
    finally:
        registry.clear()
        registry.discover_builtin()


def test_registry_selects_hcl_terraform_for_hcl_with_tf_blocks() -> None:
    """A .hcl file containing Terraform-signature blocks is claimed by the Terraform parser."""
    registry.clear()
    registry.discover_builtin()
    try:
        tf_hcl = b'terraform {\n  required_version = ">= 1.0"\n}\n'
        assert registry.select("backend.hcl", tf_hcl).name == "hcl-terraform"
    finally:
        registry.clear()
        registry.discover_builtin()


def test_registry_selects_hcl_for_non_terraform_hcl() -> None:
    """A .hcl file with no Terraform blocks (e.g. Packer) falls through to the plain hcl parser."""
    registry.clear()
    registry.discover_builtin()
    try:
        packer_hcl = b'source "amazon-ebs" "ubuntu" {\n  ami_name = "my-ami"\n}\n'
        assert registry.select("build.pkr.hcl", packer_hcl).name == "hcl"
    finally:
        registry.clear()
        registry.discover_builtin()


def test_hcl_in_capabilities_languages_and_extensions() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        caps = registry.capabilities()
        assert "hcl" in caps["languages"]
        assert "hcl-terraform" in caps["languages"]
        assert ".hcl" in caps["extensions"]
        assert ".tf" in caps["extensions"]
        assert ".tfvars" in caps["extensions"]
    finally:
        registry.clear()
        registry.discover_builtin()
