# The AWS credentials used HERE (whatever `terraform apply` picks up from
# your AWS CLI config / environment / SSO session) are a completely
# separate concept from the X.509 device certificate this configuration
# generates below for the gateway to use. These IAM-level credentials
# provision resources; they are never read by, stored in, or needed by
# the running Edge Gateway process. See docs/aws-setup.md for the full
# explanation of that boundary.

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# AWS IoT Core's per-account, per-region MQTT endpoint -- this is what
# the gateway's AWS_IOT_ENDPOINT environment variable should be set to
# (see outputs.tf).
data "aws_iot_endpoint" "current" {
  endpoint_type = "iot:Data-ATS"
}
