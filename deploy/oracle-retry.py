"""Keep asking Oracle Cloud for a free Ampere A1 VM until capacity frees up.

Free ARM capacity is often "out of capacity" in every availability domain;
it frees up at random times. This tries each AD every few minutes, prefers
4 OCPU / 24 GB, falls back to 2 OCPU / 12 GB, and stops at the first success.

Uses ~/.oci/config (API key). Creates the network (VCN, internet gateway,
public subnet, ports 22/80/443) on the first run if it doesn't exist yet.
Run:  python oracle-retry.py
"""
import os
import sys
import time
from datetime import datetime

import oci

NAME = "bkk-transit"
SHAPE = "VM.Standard.A1.Flex"
SIZES = [(4, 24), (2, 12)]          # (OCPUs, GB), preferred first
BOOT_GB = 150
ROUND_PAUSE_S = 120                  # between rounds (Oracle rate-limits launch calls)
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa.pub")

config = oci.config.from_file()
TENANCY = config["tenancy"]
compute = oci.core.ComputeClient(config)
net = oci.core.VirtualNetworkClient(config)
identity = oci.identity.IdentityClient(config)


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def ensure_network():
    """Return a public subnet id, creating VCN + gateway + subnet if needed."""
    for s in net.list_subnets(TENANCY).data:
        if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic:
            vcn = net.get_vcn(s.vcn_id).data
            sl = net.get_security_list(s.security_list_ids[0]).data
            open_ports(sl, vcn)
            ensure_gateway(vcn)
            log(f"using existing public subnet {s.display_name}")
            return s.id
    log("creating network")
    vcn = net.create_vcn(oci.core.models.CreateVcnDetails(
        compartment_id=TENANCY, cidr_blocks=["10.0.0.0/16"], display_name=f"{NAME}-vcn",
        dns_label="bkkvcn")).data
    oci.wait_until(net, net.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE")
    ensure_gateway(vcn)
    sl = net.get_security_list(vcn.default_security_list_id).data
    open_ports(sl, vcn)
    sub = net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=TENANCY, vcn_id=vcn.id, cidr_block="10.0.0.0/24",
        display_name=f"{NAME}-subnet", dns_label="bkksub",
        route_table_id=vcn.default_route_table_id,
        security_list_ids=[vcn.default_security_list_id])).data
    oci.wait_until(net, net.get_subnet(sub.id), "lifecycle_state", "AVAILABLE")
    return sub.id


def ensure_gateway(vcn):
    igws = [g for g in net.list_internet_gateways(TENANCY, vcn_id=vcn.id).data
            if g.lifecycle_state == "AVAILABLE"]
    igw = igws[0] if igws else net.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=TENANCY, vcn_id=vcn.id, is_enabled=True, display_name=f"{NAME}-igw")).data
    rt = net.get_route_table(vcn.default_route_table_id).data
    if not any(r.destination == "0.0.0.0/0" for r in rt.route_rules):
        net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(
            route_rules=rt.route_rules + [oci.core.models.RouteRule(
                destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id)]))


def open_ports(sl, vcn):
    have = {r.tcp_options.destination_port_range.min for r in sl.ingress_security_rules
            if r.tcp_options and r.tcp_options.destination_port_range and r.source == "0.0.0.0/0"}
    new = [oci.core.models.IngressSecurityRule(
        protocol="6", source="0.0.0.0/0",
        tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=p, max=p)))
        for p in (22, 80, 443) if p not in have]
    if new:
        net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=sl.ingress_security_rules + new))
        log(f"opened ports {[r.tcp_options.destination_port_range.min for r in new]}")


def ubuntu_image():
    imgs = compute.list_images(TENANCY, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                               shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC").data
    if not imgs:
        sys.exit("no Ubuntu 24.04 image for A1 found")
    log(f"image {imgs[0].display_name}")
    return imgs[0].id


def existing_instance():
    for i in compute.list_instances(TENANCY).data:
        if i.shape == SHAPE and i.lifecycle_state in ("PROVISIONING", "STARTING", "RUNNING"):
            return i


def public_ip(inst):
    for att in compute.list_vnic_attachments(TENANCY, instance_id=inst.id).data:
        ip = net.get_vnic(att.vnic_id).data.public_ip
        if ip:
            return ip


def main():
    inst = existing_instance()
    if inst:
        log(f"an A1 instance already exists: {inst.display_name} ({inst.lifecycle_state})")
    else:
        subnet = ensure_network()
        image = ubuntu_image()
        ads = [a.name for a in identity.list_availability_domains(TENANCY).data]
        key = open(SSH_KEY).read().strip()
        attempt = 0
        while not inst:
            attempt += 1
            for ocpus, mem in SIZES:
                for ad in ads:
                    try:
                        inst = compute.launch_instance(oci.core.models.LaunchInstanceDetails(
                            compartment_id=TENANCY, availability_domain=ad, display_name=NAME, shape=SHAPE,
                            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                                ocpus=ocpus, memory_in_gbs=mem),
                            source_details=oci.core.models.InstanceSourceViaImageDetails(
                                image_id=image, boot_volume_size_in_gbs=BOOT_GB),
                            create_vnic_details=oci.core.models.CreateVnicDetails(
                                subnet_id=subnet, assign_public_ip=True),
                            metadata={"ssh_authorized_keys": key})).data
                        log(f"GOT IT: {ocpus} OCPU / {mem} GB in {ad}")
                        break
                    except oci.exceptions.ServiceError as e:
                        if e.status == 429:
                            log("rate limited, slowing down")
                            time.sleep(60)
                        elif "capacity" in (e.message or "").lower():
                            pass
                        else:
                            log(f"error {e.status} {e.code}: {e.message}")
                            if e.status in (400, 401, 404) and e.code != "LimitExceeded":
                                raise
                    time.sleep(5)
                if inst:
                    break
            if not inst:
                if attempt % 10 == 1:
                    log(f"round {attempt}: out of capacity everywhere, retrying every {ROUND_PAUSE_S}s")
                time.sleep(ROUND_PAUSE_S)
    oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING", max_wait_seconds=900)
    log(f"RUNNING. Public IP: {public_ip(inst)}")


if __name__ == "__main__":
    main()
