#!/bin/bash
# Create mandate-prod. Run once in Cloud Shell as the project owner.
# Safe to run again: what already exists is left alone. Does not change IAM
# and does not print secret values.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-mandate-515200}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
NETWORK="${NETWORK:-default}"
VM_NAME="${VM_NAME:-mandate-prod}"
ADDRESS_NAME="${ADDRESS_NAME:-mandate-prod-ip}"
DATA_DISK="${DATA_DISK:-mandate-prod-data}"
TAG="${TAG:-mandate-web}"
RUNTIME_SA="${RUNTIME_SA:-mandate-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"
DEPLOY_SA="${DEPLOY_SA:-mandate-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
SNAPSHOT_POLICY="${SNAPSHOT_POLICY:-mandate-prod-daily}"
IAP_RANGE="35.235.240.0/20"

here="$(cd "$(dirname "$0")" && pwd)"
startup="${here}/startup.sh"

if [ ! -f "$startup" ]; then
  echo "create-server: missing ${startup}" >&2
  exit 1
fi

gcloud=(gcloud --project="$PROJECT_ID" --quiet)

have() {
  "${gcloud[@]}" "$@" >/dev/null 2>&1
}

echo "create-server: project ${PROJECT_ID}, zone ${ZONE}"

if have compute addresses describe "$ADDRESS_NAME" --region="$REGION"; then
  echo "create-server: address ${ADDRESS_NAME} already exists"
else
  "${gcloud[@]}" compute addresses create "$ADDRESS_NAME" \
    --region="$REGION" \
    --network-tier=PREMIUM
fi

ip="$("${gcloud[@]}" compute addresses describe "$ADDRESS_NAME" \
  --region="$REGION" --format='value(address)')"

if have compute firewall-rules describe mandate-allow-web; then
  echo "create-server: firewall mandate-allow-web already exists"
else
  # 80 and 443 only. 22, 5432, 8000 and 8001 are not in this rule.
  "${gcloud[@]}" compute firewall-rules create mandate-allow-web \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$TAG" \
    --description="HTTP and HTTPS for mandate-prod"
fi

if have compute firewall-rules describe mandate-allow-iap; then
  echo "create-server: firewall mandate-allow-iap already exists"
else
  "${gcloud[@]}" compute firewall-rules create mandate-allow-iap \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22,tcp:8443,tcp:9090 \
    --source-ranges="$IAP_RANGE" \
    --target-tags="$TAG" \
    --description="SSH and admin ports from Identity-Aware Proxy only"
fi

# The default network opens 22 and 3389 to the internet. Drop those so the
# only SSH path is the IAP rule above. Nothing else about the network changes.
for rule in default-allow-ssh default-allow-rdp; do
  if have compute firewall-rules describe "$rule"; then
    echo "create-server: removing ${rule} (it opens a port to the internet)"
    "${gcloud[@]}" compute firewall-rules delete "$rule"
  fi
done

if have compute disks describe "$DATA_DISK" --zone="$ZONE"; then
  echo "create-server: disk ${DATA_DISK} already exists"
else
  "${gcloud[@]}" compute disks create "$DATA_DISK" \
    --zone="$ZONE" \
    --size=10GB \
    --type=pd-standard
fi

if have compute instances describe "$VM_NAME" --zone="$ZONE"; then
  echo "create-server: instance ${VM_NAME} already exists"
  devices="$("${gcloud[@]}" compute instances describe "$VM_NAME" \
    --zone="$ZONE" --format='value(disks[].deviceName)')"
  if printf '%s\n' "$devices" | tr ';' '\n' | grep -qx 'mandate-data'; then
    echo "create-server: data disk already attached"
  else
    echo "create-server: attaching ${DATA_DISK}"
    "${gcloud[@]}" compute instances attach-disk "$VM_NAME" \
      --zone="$ZONE" \
      --disk="$DATA_DISK" \
      --device-name=mandate-data \
      --mode=rw
  fi
else
  "${gcloud[@]}" compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type=e2-medium \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-standard \
    --boot-disk-device-name=mandate-boot \
    --network="$NETWORK" \
    --network-tier=PREMIUM \
    --address="$ip" \
    --tags="$TAG" \
    --service-account="$RUNTIME_SA" \
    --scopes=cloud-platform \
    --shielded-secure-boot \
    --shielded-vtpm \
    --shielded-integrity-monitoring \
    --metadata=enable-oslogin=TRUE \
    --metadata-from-file="startup-script=${startup}" \
    --disk="name=${DATA_DISK},device-name=mandate-data,mode=rw,boot=no,auto-delete=no"
fi

if have compute resource-policies describe "$SNAPSHOT_POLICY" --region="$REGION"; then
  echo "create-server: snapshot schedule ${SNAPSHOT_POLICY} already exists"
else
  "${gcloud[@]}" compute resource-policies create snapshot-schedule "$SNAPSHOT_POLICY" \
    --region="$REGION" \
    --max-retention-days=14 \
    --start-time=09:00 \
    --daily-schedule \
    --on-source-disk-delete=keep-auto-snapshots \
    --storage-location="$REGION"
fi

attach_policy() {
  local disk="$1"
  local policies
  policies="$("${gcloud[@]}" compute disks describe "$disk" \
    --zone="$ZONE" --format='value(resourcePolicies)')"
  case "$policies" in
    *"${SNAPSHOT_POLICY}"*)
      echo "create-server: ${disk} already has ${SNAPSHOT_POLICY}"
      ;;
    *)
      "${gcloud[@]}" compute disks add-resource-policies "$disk" \
        --zone="$ZONE" \
        --resource-policies="$SNAPSHOT_POLICY"
      ;;
  esac
}

boot_url="$("${gcloud[@]}" compute instances describe "$VM_NAME" \
  --zone="$ZONE" --format='value(disks[0].source)')"
attach_policy "${boot_url##*/}"
attach_policy "$DATA_DISK"

echo
echo "static ip: ${ip}"
echo
echo "GCP_PROJECT_ID=${PROJECT_ID}"
echo "GCP_DEPLOY_SA=${DEPLOY_SA}"
echo "GCE_INSTANCE=${VM_NAME}"
echo "GCE_ZONE=${ZONE}"
echo "GCE_DEPLOY_DIR=/srv/mandate/app"
echo "GAR_LOCATION=${REGION}"
echo "GAR_REPOSITORY=mandate"
echo "GCP_WIF_PROVIDER=<paste the provider resource from the bootstrap>"
echo
echo "GoDaddy A record: host api, value ${ip}"
