"""The infra provider tests import ``breezeai_cog.infra.aws.s3``, which needs
botocore/boto3 from the ``[server]`` / ``[all]`` extra. Skip the whole package
on a base install rather than failing collection."""

import pytest

pytest.importorskip("botocore", reason="requires the [server] or [all] extra (boto3)")
