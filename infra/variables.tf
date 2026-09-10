variable "aws_region" {
  description = "AWS region to provision the IoT Core resources in."
  type        = string
  default     = "ap-south-1"
}

variable "thing_name" {
  description = <<-EOT
    Name of the AWS IoT "Thing" representing this project's one simulated
    vehicle (DEFAULT_VEHICLE_ID in common/telemetry_schema.py is a
    separate, application-level identifier -- this is only the AWS IoT
    resource name, and the two are not required to match).
  EOT
  type        = string
  default     = "edge-to-cloud-telemetry-poc-vehicle"
}

variable "cloudwatch_log_retention_days" {
  description = "How long the IoT Rule's routed telemetry stays in CloudWatch Logs. Short by default -- this is a POC, not a system meant to retain data indefinitely (see docs/assumptions-and-limitations.md)."
  type        = number
  default     = 14
}

variable "certs_output_dir" {
  description = <<-EOT
    Local directory Terraform writes the generated device certificate and
    private key into. Never committed to git -- see .gitignore's
    infra/certs/ entry. Point the gateway's AWS_IOT_CERT_PATH/
    AWS_IOT_KEY_PATH env vars at the files this produces.
  EOT
  type        = string
  default     = "./certs"
}
