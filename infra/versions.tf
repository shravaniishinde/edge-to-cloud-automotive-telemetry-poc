# Phase 5: minimal AWS IoT Core provisioning for this POC. Kept
# deliberately small -- see ../docs/aws-setup.md for the full walkthrough
# and ../ARCHITECTURE.md for why this is the extent of it (no Kinesis,
# no DynamoDB/S3, see the "Kinesis excluded" / "Kinesis deferred" decision
# rows).

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }
}
