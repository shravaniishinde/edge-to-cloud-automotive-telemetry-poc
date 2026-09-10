# --- Where routed telemetry lands ---
# This is the ONE downstream destination for Phase 5, per the locked-in
# decision to defer Kinesis (see ARCHITECTURE.md). Proving telemetry
# reached AWS is fully demonstrable from these logs alone.
resource "aws_cloudwatch_log_group" "telemetry" {
  name              = "/aws/iot/${var.thing_name}/telemetry"
  retention_in_days = var.cloudwatch_log_retention_days
}

# --- The role the IoT Rule assumes to write to that log group ---
data "aws_iam_policy_document" "iot_rule_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "iot_rule_cloudwatch" {
  name               = "${var.thing_name}-iot-rule-cloudwatch"
  assume_role_policy = data.aws_iam_policy_document.iot_rule_assume.json
}

# Scoped to exactly this one log group -- not logs:* on *.
data "aws_iam_policy_document" "iot_rule_cloudwatch_write" {
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.telemetry.arn}:*"]
  }
}

resource "aws_iam_role_policy" "iot_rule_cloudwatch_write" {
  name   = "${var.thing_name}-iot-rule-cloudwatch-write"
  role   = aws_iam_role.iot_rule_cloudwatch.id
  policy = data.aws_iam_policy_document.iot_rule_cloudwatch_write.json
}

# --- The IoT Rule itself ---
# SQL matches this project's one existing topic scheme exactly
# (edge_gateway/normalization.py's build_topic():
# vehicle/{vehicle_id}/telemetry/{source_ecu}/{signal_name}) -- no topic
# or schema change was needed to make AWS IoT Core work.
#
# Kinesis is deliberately NOT wired up here (see ARCHITECTURE.md's Phase
# 5 decision row and docs/assumptions-and-limitations.md) -- if it's ever
# justified, it's a second `cloudwatch_logs`-style block added to this
# same resource, routing the same rule to a Kinesis stream. The Edge
# Gateway code would not change at all for that -- routing is entirely
# an AWS-side concern, which is the whole point of using an IoT Rule
# instead of making the gateway responsible for a second destination.
resource "aws_iot_topic_rule" "telemetry_to_cloudwatch" {
  name        = replace("${var.thing_name}_telemetry_to_cloudwatch", "-", "_")
  description = "Routes simulated vehicle telemetry to CloudWatch Logs (Phase 5)."
  enabled     = true
  sql         = "SELECT * FROM 'vehicle/+/telemetry/+/+'"
  sql_version = "2016-03-23"

  cloudwatch_logs {
    log_group_name = aws_cloudwatch_log_group.telemetry.name
    role_arn       = aws_iam_role.iot_rule_cloudwatch.arn
  }
}
