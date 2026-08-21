"""TerraformParser: statement emission (gated by capture_statements), externalImports,
ignore patterns, and registry integration."""

from __future__ import annotations

import json

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.terraform.parser import TerraformParser

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


def _parse(path: str, source: bytes, *, capture_statements: bool = False):
    ctx = ParseContext(
        path=path,
        abs_path=None,
        source=source,
        repo_root=".",
        capture_statements=capture_statements,
    )
    return TerraformParser().parse_file(ctx)


# ── FileRecord shape ──────────────────────────────────────────────────────────


def test_emits_config_record() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert rec.type == "config"
    assert rec.language == "hcl"
    assert rec.framework == "terraform"


def test_no_metadata() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert rec.metadata is None


def test_no_functions_or_classes() -> None:
    rec = _parse("main.tf", _TF_SRC)
    assert rec.functions == []
    assert rec.classes == []


def test_loc_positive() -> None:
    assert _parse("main.tf", _TF_SRC).loc > 0


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
    assert any(s.semanticType == "iac_data_source" for s in rec.statements)


def test_variable_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_variable" for s in rec.statements)


def test_output_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_output" for s in rec.statements)


def test_module_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_module" for s in rec.statements)


def test_provider_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_provider" for s in rec.statements)


def test_locals_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_local" for s in rec.statements)


def test_terraform_block_semantic_type() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert any(s.semanticType == "iac_settings" for s in rec.statements)


def test_tfvars_semantic_type_is_variable_value() -> None:
    rec = _parse("prod.auto.tfvars", _TFVARS_SRC, capture_statements=True)
    assert all(s.semanticType == "iac_variable_value" for s in rec.statements)


# ── statement name (address) ──────────────────────────────────────────────────


def test_resource_statement_name_is_address() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    names = {s.name for s in rec.statements if s.semanticType == "iac_resource"}
    assert "aws_s3_bucket.my_bucket" in names
    assert "aws_instance.web" in names


def test_data_statement_name_is_address() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data_stmt = next(s for s in rec.statements if s.semanticType == "iac_data_source")
    assert data_stmt.name == "aws_ami.ubuntu"


def test_variable_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    var_stmt = next(s for s in rec.statements if s.semanticType == "iac_variable")
    assert var_stmt.name == "region"


def test_output_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    out_stmt = next(s for s in rec.statements if s.semanticType == "iac_output")
    assert out_stmt.name == "bucket_arn"


def test_module_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    mod_stmts = {s.name for s in rec.statements if s.semanticType == "iac_module"}
    assert "vpc" in mod_stmts
    assert "local_mod" in mod_stmts


def test_provider_statement_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    prov = next(s for s in rec.statements if s.semanticType == "iac_provider")
    assert prov.name == "aws"


def test_locals_statement_has_no_name() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    loc_stmt = next(s for s in rec.statements if s.semanticType == "iac_local")
    assert loc_stmt.name is None


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
    bucket = next(s for s in rec.statements if s.name == "aws_s3_bucket.my_bucket")
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


def test_statement_text_is_full_block_source() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    for stmt in rec.statements:
        assert "{" in stmt.text and "}" in stmt.text


# ── platform ──────────────────────────────────────────────────────────────────


def test_aws_resource_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform == "aws" for s in resources)


def test_aws_data_source_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    data_stmts = [s for s in rec.statements if s.semanticType == "iac_data_source"]
    assert all(s.platform == "aws" for s in data_stmts)


def test_provider_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    prov = next(s for s in rec.statements if s.semanticType == "iac_provider")
    assert prov.platform == "aws"


def test_azure_resource_platform() -> None:
    rec = _parse("azure.tf", _AZURE_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform == "azure" for s in resources)


def test_generic_resource_no_platform() -> None:
    rec = _parse("generic.tf", _GENERIC_SRC, capture_statements=True)
    resources = [s for s in rec.statements if s.semanticType == "iac_resource"]
    assert all(s.platform is None for s in resources)


def test_non_resource_blocks_no_platform() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    no_platform = [s for s in rec.statements if s.semanticType in ("iac_variable", "iac_output", "iac_local", "iac_settings", "iac_module")]
    assert all(s.platform is None for s in no_platform)


def test_file_platform_aws() -> None:
    rec = _parse("main.tf", _TF_SRC, capture_statements=True)
    assert rec.platform == "aws"


def test_file_platform_azure() -> None:
    rec = _parse("azure.tf", _AZURE_SRC, capture_statements=True)
    assert rec.platform == "azure"


def test_file_platform_none_for_generic() -> None:
    rec = _parse("generic.tf", _GENERIC_SRC, capture_statements=True)
    assert rec.platform is None


def test_file_platform_none_without_capture_statements() -> None:
    # statements not captured → no platform signal
    rec = _parse("main.tf", _TF_SRC, capture_statements=False)
    assert rec.platform is None


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


# ── ignore patterns ───────────────────────────────────────────────────────────


def test_ignore_patterns_include_terraform_dir() -> None:
    assert any(".terraform/" in p for p in TerraformParser().ignore_patterns())


def test_ignore_patterns_include_override() -> None:
    assert any("override.tf" in p for p in TerraformParser().ignore_patterns())


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
    assert "metadata" not in data


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


# ── registry integration ──────────────────────────────────────────────────────


def test_registry_selects_terraform_for_tf() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        assert registry.select("infra/main.tf", b"").name == "terraform"
    finally:
        registry.clear()
        registry.discover_builtin()


def test_registry_selects_terraform_for_tfvars() -> None:
    registry.clear()
    registry.discover_builtin()
    try:
        assert registry.select("prod.tfvars", b"region = \"us-east-1\"").name == "terraform"
    finally:
        registry.clear()
        registry.discover_builtin()
