# --- The Thing: one AWS IoT resource representing this POC's one simulated vehicle ---
resource "aws_iot_thing" "vehicle" {
  name = var.thing_name
}

# --- The device certificate ---
# active = true with no CSR tells AWS IoT Core to generate a brand-new
# key pair AND certificate for us in one step -- the simplest path for a
# dev/portfolio POC, and why this resource alone is enough (no separate
# tls_private_key/CSR resources needed).
#
# Production-grade device provisioning would do this differently: each
# real device generates its OWN private key locally and sends AWS only a
# Certificate Signing Request, so the private key never leaves that
# device, not even briefly inside a Terraform state file. This project
# deliberately uses the simpler "let AWS generate it" flow instead,
# documented here (and in docs/aws-setup.md) as a known, intentional
# POC-only simplification -- see docs/assumptions-and-limitations.md's
# Phase 5 entry.
resource "aws_iot_certificate" "device" {
  active = true
}

resource "aws_iot_thing_principal_attachment" "device" {
  thing     = aws_iot_thing.vehicle.name
  principal = aws_iot_certificate.device.arn
}

# --- Least-privilege IoT policy ---
# This certificate may only connect as this one Thing and publish to this
# project's own topic prefix (see edge_gateway/normalization.py's
# build_topic()) -- nothing broader. No iot:Subscribe/iot:Receive is
# granted because the gateway only ever publishes; it never subscribes.
data "aws_iam_policy_document" "device" {
  statement {
    sid       = "Connect"
    effect    = "Allow"
    actions   = ["iot:Connect"]
    resources = ["arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:client/${var.thing_name}"]
  }

  statement {
    sid       = "PublishTelemetry"
    effect    = "Allow"
    actions   = ["iot:Publish"]
    resources = ["arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/vehicle/*"]
  }
}

resource "aws_iot_policy" "device" {
  name   = "${var.thing_name}-device-policy"
  policy = data.aws_iam_policy_document.device.json
}

resource "aws_iot_policy_attachment" "device" {
  policy = aws_iot_policy.device.name
  target = aws_iot_certificate.device.arn
}

# --- Write the generated credentials to local files for the gateway ---
# These are DEV-CONVENIENCE files, written outside of git (see
# .gitignore's infra/certs/ entry). The public certificate is not
# sensitive; local_sensitive_file (rather than local_file) is used for
# the private key specifically so it never appears in `terraform plan`/
# `terraform show` output.
resource "local_file" "device_cert" {
  filename        = "${var.certs_output_dir}/device-certificate.pem.crt"
  content         = aws_iot_certificate.device.certificate_pem
  file_permission = "0600"
}

resource "local_sensitive_file" "device_key" {
  filename        = "${var.certs_output_dir}/device-private.pem.key"
  content         = aws_iot_certificate.device.private_key
  file_permission = "0600"
}
