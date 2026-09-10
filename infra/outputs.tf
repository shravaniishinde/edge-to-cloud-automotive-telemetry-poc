output "aws_iot_endpoint" {
  description = "Set the gateway's AWS_IOT_ENDPOINT environment variable to this value."
  value       = data.aws_iot_endpoint.current.endpoint_address
}

output "device_certificate_path" {
  description = "Set the gateway's AWS_IOT_CERT_PATH environment variable to this value."
  value       = local_file.device_cert.filename
}

output "device_private_key_path" {
  description = "Set the gateway's AWS_IOT_KEY_PATH environment variable to this value. The file itself is sensitive; this output is just its path, not its content."
  value       = local_sensitive_file.device_key.filename
}

output "cloudwatch_log_group" {
  description = "Where to look in the AWS Console (or `aws logs tail`) to confirm telemetry is arriving."
  value       = aws_cloudwatch_log_group.telemetry.name
}

output "thing_name" {
  description = "The AWS IoT Thing name provisioned -- useful for `aws iot` CLI commands or the console's MQTT test client."
  value       = aws_iot_thing.vehicle.name
}
