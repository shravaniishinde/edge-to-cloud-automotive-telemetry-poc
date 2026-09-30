# AWS IoT Core Setup (Phase 5)

How to connect the Edge Gateway to a real AWS IoT Core endpoint instead
of local Mosquitto, reproducibly, from a clean AWS account. Local
Mosquitto (Phases 1-4) remains the default and requires none of this --
skip this document entirely if that's all you need.

## Two separate credential concepts -- read this first

This project deliberately keeps two things apart, and it's worth being
explicit about the difference before touching anything:

- **AWS (IAM) credentials** -- whatever your AWS CLI / SSO session /
  environment provides when you run `terraform apply` below. These
  provision AWS resources (create the Thing, certificate, policy, rule).
  They are used once, by you, on your own machine, at setup time.
- **The X.509 device certificate** -- generated *by* that `terraform
  apply` (see `infra/iot.tf`), then used *by the running Edge Gateway
  process* to authenticate its MQTT connection to AWS IoT Core. This is
  the only credential the gateway itself ever sees.

The gateway process never holds, needs, or reads an AWS access key. If
you're explaining this in an interview: "provisioning-time credentials"
and "runtime device credentials" are two different trust boundaries, and
conflating them is a common real-world IoT security mistake this project
deliberately avoids.

## Prerequisites

- An AWS account (a personal/free-tier one is fine -- see "Cost" below).
- AWS CLI configured locally (`aws configure`, or SSO) with permissions
  to create IoT Things/certificates/policies, an IAM role, and a
  CloudWatch log group.
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5.

## 1. Provision AWS IoT Core with Terraform

```bash
cd infra
terraform init
terraform apply
```

Review the plan, then confirm. This creates:

- One AWS IoT **Thing** (`aws_iot_thing.vehicle`) representing this
  project's one simulated vehicle.
- One AWS IoT **certificate** (`aws_iot_certificate.device`) -- AWS
  generates a fresh key pair and certificate for you (see the note in
  `infra/iot.tf` about why this project uses that simpler flow instead
  of a device-generated CSR, and what a production fleet would do
  differently).
- One narrowly-scoped **IoT policy**, allowing this certificate to
  `iot:Connect` as this one Thing and `iot:Publish` only under the
  `vehicle/*` topic prefix -- nothing else.
- One **IoT Rule** (SQL: `SELECT * FROM 'vehicle/+/telemetry/+/+'`)
  routing matching telemetry to one **CloudWatch Logs** group. Kinesis
  is deliberately not provisioned in this phase -- see
  `ARCHITECTURE.md`'s Phase 5 decision row.

`terraform apply` writes the generated certificate and private key to
`infra/certs/` (gitignored) and prints the values you need next.

## 2. Download the AWS root CA

AWS IoT Core's server certificate chains up to Amazon's own root CA,
which is a fixed public file (not something Terraform provisions) --
download it once:

```bash
curl -o infra/certs/AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
```

## 3. Configure the gateway

Copy the values `terraform apply` printed (or re-run `terraform output`)
into your `.env` (see `.env.example` for the full list of variable
names):

```bash
AWS_IOT_ENDPOINT=<terraform output -raw aws_iot_endpoint>
AWS_IOT_CA_PATH=./infra/certs/AmazonRootCA1.pem
AWS_IOT_CERT_PATH=<terraform output -raw device_certificate_path>
AWS_IOT_KEY_PATH=<terraform output -raw device_private_key_path>
AWS_IOT_CLIENT_ID=edge-to-cloud-telemetry-poc-vehicle
```

The first four must be set together. `edge_gateway/cloud_publisher.py`
treats any other combination (some set, some not) as a configuration
error and refuses to start -- it will never silently connect to local
Mosquitto instead. See that module's docstring for the exact rule.

**`AWS_IOT_CLIENT_ID` is also required in practice.** The IoT policy in
`infra/iot.tf` only allows `iot:Connect` for the MQTT client ID equal to
the Thing name (`edge-to-cloud-telemetry-poc-vehicle`). The loader treats
the client ID as optional, and if it is unset paho-mqtt connects with a
generated ID, which AWS IoT Core rejects. The symptom is a publish that
is never acknowledged, not a clear error.

Nothing in the project loads `.env` automatically. Export the variables
in the shell that runs the command. In PowerShell, from the repository
root (the certificate paths are relative to it), this loads only the
`AWS_IOT_*` lines from `.env` without printing them:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*AWS_IOT_[A-Z_]+\s*=' } | ForEach-Object { $n, $v = $_ -split '=', 2; Set-Item -Path "Env:$($n.Trim())" -Value $v.Trim() }
```

Clear them afterwards so later runs use local Mosquitto again:

```powershell
Get-ChildItem Env:AWS_IOT_* | Remove-Item
```

## 3a. Smoke-test the connection (optional)

`edge_gateway/tests/test_aws_iot_integration.py` opens a mutual-TLS
connection with the same `MqttPublisher` the gateway uses and publishes
one QoS 1 message, `{"smoke_test": true}`, to
`vehicle/SIM-VEHICLE-01/telemetry/powertrain/vehicle_speed_kph`. It passes
only if AWS IoT Core acknowledges the publish. It skips when the
`AWS_IOT_*` variables aren't exported, which is why it always skips in CI.

In AWS Console -> IoT Core -> MQTT test client, subscribe to `vehicle/#`
first, then, with the variables exported:

```powershell
.\.venv\Scripts\python.exe -m pytest edge_gateway/tests/test_aws_iot_integration.py -v -rs
```

Expected: `1 passed`, and the test client shows `{"smoke_test": true}` on
that topic. The topic matches the IoT Rule, so the message also lands in
the CloudWatch log group. `SKIPPED` means the variables weren't exported
in that shell; a failed assertion usually means `AWS_IOT_CLIENT_ID` is
missing or the certificate isn't active and attached in AWS.

## 4. Run the gateway against AWS

```bash
python run_demo.py --duration 30
```

With the four `AWS_IOT_*` variables set in your environment, this
connects to AWS IoT Core instead of local Mosquitto -- nothing else
about `run_demo.py`'s usage changes; the 3 simulated ECUs, the ingest ->
validate -> normalize pipeline, and Phase 4's buffering/reconnect logic
are all identical to the local-Mosquitto case, because they're the exact
same code. The same `vehicle/{vehicle_id}/telemetry/{source_ecu}/{signal_name}`
topics and the same `TelemetryEvent` JSON payload are published either
way.

## 5. Verify telemetry is arriving

Either:

- AWS Console -> IoT Core -> MQTT test client -> subscribe to `vehicle/#`, or
- AWS Console -> CloudWatch -> Log groups -> the group named in
  `terraform output cloudwatch_log_group`, or
- `aws logs tail <that log group name> --follow`

## 6. Tear down

This is a POC, not something meant to run continuously (see
`docs/assumptions-and-limitations.md`). When you're done:

```bash
cd infra
terraform destroy
```

This removes the Thing, certificate, policy, IoT Rule, IAM role, and
CloudWatch log group. It does not delete `infra/certs/*.pem` files
locally -- remove those yourself if you want a fully clean slate.

## Cost

At this POC's message volume, AWS IoT Core and CloudWatch Logs both stay
well within AWS's free tier for any reasonable demo/testing session.
Nothing here provisions an always-on, continuously-billed resource --
which is exactly why Kinesis (a provisioned shard bills whether or not
it's used) was deliberately left out of this phase. Run `terraform
destroy` when you're done with a session regardless -- see "Tear down"
above.

## Dev vs. production device provisioning

Worth stating plainly, since this project borrows real IoT concepts for
demonstration: `infra/iot.tf`'s `aws_iot_certificate` with `active = true`
and no CSR is a convenience appropriate for a single developer's own POC.
A production fleet would instead have each physical device generate its
own private key ON the device (e.g. in a hardware secure element) and
send AWS only a Certificate Signing Request, so the private key never
exists anywhere Terraform (or its state file) can see it. This project
uses the simpler flow deliberately -- see `docs/assumptions-and-limitations.md`'s
Phase 5 entry -- because demonstrating the AWS IoT Core connection and
topic/rule/logging architecture is the point here, not building a
production device-provisioning pipeline.
