#!/usr/bin/env python3
"""Bulk-analyze all JIRA issues and populate tracking.json with
structured analysis, repro_steps, and test_coverage for every issue.

Output format: bullet points, exact commands, summary at top, no paragraphs.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ceph_prio_hub.tracker.tracking import TrackingDB

STATE_PATH = Path.home() / ".ceph-prio-hub" / "state" / "issues.json"

COMPONENT_INFO = {
    "ceph-dashboard": {
        "area": "Ceph Dashboard (mgr module)",
        "log_cmd": "cephadm shell -- ceph log last ceph-mgr",
        "test_suite": "qa/tasks/mgr/dashboard",
        "debug_cmds": [
            "ceph mgr module ls",
            "ceph dashboard status",
            "ceph config get mgr mgr/dashboard/ssl",
        ],
        "deploy_steps": [
            "Deploy cluster with MGR and dashboard enabled",
            "Access dashboard: https://<mgr-host>:8443",
            "Verify dashboard module: `ceph mgr module enable dashboard`",
        ],
    },
    "management & ui": {
        "area": "Ceph Dashboard / Management UI",
        "log_cmd": "cephadm shell -- ceph log last ceph-mgr",
        "test_suite": "qa/tasks/mgr/dashboard",
        "debug_cmds": [
            "ceph mgr module ls",
            "ceph dashboard status",
        ],
        "deploy_steps": [
            "Deploy cluster with MGR and dashboard enabled",
            "Access dashboard: https://<mgr-host>:8443",
        ],
    },
    "rgw": {
        "area": "RADOS Gateway (S3/Swift)",
        "log_cmd": "cephadm shell -- ceph log last ceph-rgw",
        "test_suite": "qa/suites/rgw, s3-tests",
        "debug_cmds": [
            "ceph orch ps --daemon-type rgw",
            "radosgw-admin user list",
            "radosgw-admin sync status",
            "ceph config get rgw rgw_frontends",
        ],
        "deploy_steps": [
            "Deploy cluster with RGW service",
            "Create test user: `radosgw-admin user create --uid=testuser --display-name='Test'`",
            "Verify endpoint: `curl -k https://<rgw-host>:443`",
        ],
    },
    "rgw-multisite": {
        "area": "RGW Multisite Replication",
        "log_cmd": "cephadm shell -- ceph log last ceph-rgw",
        "test_suite": "qa/suites/rgw/multisite",
        "debug_cmds": [
            "radosgw-admin sync status",
            "radosgw-admin data sync status --source-zone=<zone>",
            "radosgw-admin metadata sync status",
            "radosgw-admin realm list",
            "radosgw-admin zonegroup get",
        ],
        "deploy_steps": [
            "Deploy multisite RGW with primary and secondary zones",
            "Verify sync: `radosgw-admin sync status`",
            "Create test bucket and objects on primary",
        ],
    },
    "cephadm": {
        "area": "Cephadm Orchestrator",
        "log_cmd": "cephadm shell -- ceph log last cephadm",
        "test_suite": "qa/suites/cephadm, cephci deploy suites",
        "debug_cmds": [
            "ceph orch ls",
            "ceph orch ps",
            "ceph orch host ls",
            "ceph health detail",
            "ceph versions",
        ],
        "deploy_steps": [
            "Bootstrap cluster: `cephadm bootstrap --mon-ip=<ip>`",
            "Add hosts: `ceph orch host add <hostname> <ip>`",
            "Verify services: `ceph orch ls`",
        ],
    },
    "rados": {
        "area": "RADOS (core storage layer)",
        "log_cmd": "cephadm shell -- ceph log last ceph-osd",
        "test_suite": "qa/suites/rados",
        "debug_cmds": [
            "ceph osd tree",
            "ceph osd pool ls detail",
            "ceph pg stat",
            "ceph health detail",
            "ceph crash ls-new",
            "ceph osd perf",
        ],
        "deploy_steps": [
            "Deploy cluster with OSDs on all storage nodes",
            "Create test pool: `ceph osd pool create testpool 32`",
            "Verify PG health: `ceph pg stat`",
        ],
    },
    "cephfs": {
        "area": "CephFS (distributed filesystem)",
        "log_cmd": "cephadm shell -- ceph log last ceph-mds",
        "test_suite": "qa/suites/fs",
        "debug_cmds": [
            "ceph fs status",
            "ceph mds stat",
            "ceph fs subvolumegroup ls <fsname>",
            "ceph fs subvolume ls <fsname> <group>",
        ],
        "deploy_steps": [
            "Deploy cluster with MDS daemons",
            "Create filesystem: `ceph fs volume create <fsname>`",
            "Mount via kernel: `mount -t ceph <mon>:/ /mnt/cephfs -o name=admin`",
        ],
    },
    "rbd": {
        "area": "RADOS Block Device",
        "log_cmd": "cephadm shell -- ceph log last ceph-osd",
        "test_suite": "qa/suites/rbd",
        "debug_cmds": [
            "rbd ls <pool>",
            "rbd info <pool>/<image>",
            "rbd status <pool>/<image>",
            "rbd mirror pool status <pool>",
        ],
        "deploy_steps": [
            "Deploy cluster with OSD pool for RBD",
            "Create pool: `ceph osd pool create rbdpool 32`",
            "Enable RBD app: `ceph osd pool application enable rbdpool rbd`",
            "Create image: `rbd create --size 1G rbdpool/testimg`",
        ],
    },
    "smb": {
        "area": "SMB/Samba Gateway",
        "log_cmd": "cephadm shell -- ceph log last smbd",
        "test_suite": "qa/suites/smb",
        "debug_cmds": [
            "ceph smb cluster ls",
            "ceph smb share ls <cluster>",
            "smbclient -L //<host>/ -U <user>",
        ],
        "deploy_steps": [
            "Deploy cluster with CephFS and SMB gateway",
            "Configure AD integration",
            "Create SMB cluster: `ceph smb cluster create <name> <domain>`",
            "Create share: `ceph smb share create <cluster> <share> <cephfs> <path>`",
        ],
    },
    "smb/samba": {
        "area": "SMB/Samba Gateway",
        "log_cmd": "cephadm shell -- ceph log last smbd",
        "test_suite": "qa/suites/smb",
        "debug_cmds": [
            "ceph smb cluster ls",
            "smbclient -L //<host>/ -U <user>",
        ],
        "deploy_steps": [
            "Deploy cluster with CephFS and SMB gateway",
            "Configure AD integration",
        ],
    },
    "nfs": {
        "area": "NFS-Ganesha Gateway",
        "log_cmd": "cephadm shell -- ceph log last nfs",
        "test_suite": "qa/suites/nfs",
        "debug_cmds": [
            "ceph nfs cluster ls",
            "ceph nfs export ls <cluster>",
            "showmount -e <nfs-host>",
        ],
        "deploy_steps": [
            "Deploy NFS-Ganesha: `ceph nfs cluster create <name> <placement>`",
            "Create export: `ceph nfs export create cephfs <cluster> <pseudo> <fsname>`",
            "Mount on client: `mount -t nfs <host>:<pseudo> /mnt/nfs`",
        ],
    },
    "nfs-ganesha": {
        "area": "NFS-Ganesha Gateway",
        "log_cmd": "cephadm shell -- ceph log last nfs",
        "test_suite": "qa/suites/nfs",
        "debug_cmds": [
            "ceph nfs cluster ls",
            "ceph nfs export ls <cluster>",
            "showmount -e <nfs-host>",
        ],
        "deploy_steps": [
            "Deploy NFS-Ganesha: `ceph nfs cluster create <name> <placement>`",
            "Create export",
            "Mount on client: `mount -t nfs <host>:<pseudo> /mnt/nfs`",
        ],
    },
    "nfs-ganesha/ceph": {
        "area": "NFS-Ganesha Gateway (CephFS-backed)",
        "log_cmd": "cephadm shell -- ceph log last nfs",
        "test_suite": "qa/suites/nfs",
        "debug_cmds": [
            "ceph nfs cluster ls",
            "ceph nfs export ls <cluster>",
            "ceph fs status",
        ],
        "deploy_steps": [
            "Deploy CephFS and NFS-Ganesha service",
            "Create CephFS-backed NFS export",
            "Mount on client and verify I/O",
        ],
    },
    "ceph-mgr/orchestrator": {
        "area": "Ceph MGR Orchestrator module",
        "log_cmd": "cephadm shell -- ceph log last ceph-mgr",
        "test_suite": "qa/suites/cephadm",
        "debug_cmds": [
            "ceph orch status",
            "ceph orch ls --service_type=<type>",
            "ceph orch ps",
        ],
        "deploy_steps": [
            "Deploy cluster with cephadm orchestrator",
            "Verify orchestrator: `ceph orch status`",
        ],
    },
    "ceph-mgr/crash": {
        "area": "Ceph MGR Crash module",
        "log_cmd": "cephadm shell -- ceph log last ceph-mgr",
        "test_suite": "qa/suites/rados",
        "debug_cmds": [
            "ceph crash ls-new",
            "ceph crash info <crash-id>",
            "ceph mgr module ls",
        ],
        "deploy_steps": [
            "Deploy cluster with MGR",
            "Verify crash module: `ceph mgr module enable crash`",
        ],
    },
    "ceph-mgr/core": {
        "area": "Ceph MGR Core",
        "log_cmd": "cephadm shell -- ceph log last ceph-mgr",
        "test_suite": "qa/suites/rados",
        "debug_cmds": [
            "ceph mgr stat",
            "ceph mgr module ls",
            "ceph health detail",
        ],
        "deploy_steps": [
            "Deploy cluster with MGR daemons",
            "Verify MGR: `ceph mgr stat`",
        ],
    },
    "ceph-mgr/prometheus": {
        "area": "Ceph MGR Prometheus module",
        "log_cmd": "cephadm shell -- ceph log last ceph-mgr",
        "test_suite": "qa/suites/rados",
        "debug_cmds": [
            "ceph mgr module ls",
            "curl http://<mgr>:9283/metrics",
        ],
        "deploy_steps": [
            "Deploy cluster with Prometheus module enabled",
            "Verify metrics: `curl http://<mgr>:9283/metrics | head`",
        ],
    },
    "ceph-ansible": {
        "area": "Ceph Ansible Deployment",
        "log_cmd": "journalctl -u ceph-ansible",
        "test_suite": "ceph-ansible CI",
        "debug_cmds": [
            "ansible --version",
            "ceph --version",
        ],
        "deploy_steps": [
            "Setup ansible inventory for target hosts",
            "Run preflight: `ansible-playbook cephadm-preflight.yml`",
            "Deploy cluster via cephadm",
        ],
    },
    "ceph-volume": {
        "area": "ceph-volume OSD provisioning",
        "log_cmd": "cephadm shell -- ceph-volume lvm list",
        "test_suite": "qa/suites/ceph-volume",
        "debug_cmds": [
            "ceph-volume lvm list",
            "ceph-volume inventory",
            "ceph osd tree",
        ],
        "deploy_steps": [
            "Prepare storage devices on OSD hosts",
            "Create OSDs: `ceph orch daemon add osd <host>:<device>`",
            "Verify: `ceph osd tree`",
        ],
    },
    "nvmeof": {
        "area": "NVMe-oF Gateway",
        "log_cmd": "cephadm shell -- ceph log last nvmeof",
        "test_suite": "qa/suites/nvmeof",
        "debug_cmds": [
            "ceph orch ps --daemon-type nvmeof",
            "ceph nvme-gw show",
        ],
        "deploy_steps": [
            "Deploy cluster with NVMe-oF gateway service",
            "Create NVMe-oF subsystem and namespace",
            "Connect initiator and verify I/O",
        ],
    },
}

PRIORITY_MAP = {
    "blocker": "P0",
    "critical": "P0",
    "major": "P1",
    "normal": "P2",
    "minor": "P3",
    "trivial": "P3",
}


def _extract_errors(timeline: list[dict]) -> list[str]:
    errors = []
    pat = re.compile(r"(error|exception|traceback|assert|crash|fail|segfault|HEALTH_WARN|HEALTH_ERR|core dump)", re.I)
    for entry in timeline:
        text = entry.get("summary", "")
        if pat.search(text):
            snippet = text[:250].strip()
            if snippet and snippet not in errors:
                errors.append(snippet)
    return errors[:4]


def _extract_dev_analysis(timeline: list[dict]) -> list[str]:
    hints = []
    pat = re.compile(r"(root cause|caused by|the issue is|the problem is|regression of|introduced in|fix is|fixed in|backport|the fix|this is a bug|this is due to|workaround)", re.I)
    for entry in timeline:
        text = entry.get("summary", "")
        if pat.search(text):
            snippet = text[:300].strip()
            if snippet not in hints:
                hints.append(snippet)
    return hints[:3]


def _extract_repro_hints(timeline: list[dict]) -> list[str]:
    hints = []
    pat = re.compile(r"(step|reproduce|to trigger|how to|when we|when you|if you|tried to|attempt)", re.I)
    for entry in timeline:
        text = entry.get("summary", "")
        if pat.search(text):
            snippet = text[:250].strip()
            if snippet not in hints:
                hints.append(snippet)
    return hints[:3]


def _get_versions(issue: dict) -> str:
    versions = issue.get("ceph_versions", [])
    if versions:
        return ", ".join(versions)
    ver_match = re.findall(r"(\d+\.\d+(?:z\d+)?)", issue.get("subject", ""))
    return ", ".join(ver_match) if ver_match else "Not specified"


def _get_bz_ids(issue: dict) -> list[str]:
    bz = set()
    for text in [issue.get("subject", "")] + [e.get("summary", "") for e in issue.get("timeline", [])]:
        bz.update(re.findall(r"BZ#(\d+)", text, re.I))
        bz.update(re.findall(r"bugzilla\.redhat\.com/show_bug\.cgi\?id=(\d+)", text))
    return list(bz)[:5]


def build_analysis(issue: dict) -> str:
    jira_key = issue["jira_ids"][0] if issue.get("jira_ids") else issue["issue_id"]
    subject = issue.get("subject", "")
    components = issue.get("components", [])
    severity = issue.get("severity", "normal")
    versions = _get_versions(issue)
    bz_ids = _get_bz_ids(issue)
    primary_comp = components[0] if components else "unknown"
    comp_info = COMPONENT_INFO.get(primary_comp, {})

    dev_hints = _extract_dev_analysis(issue.get("timeline", []))
    errors = _extract_errors(issue.get("timeline", []))

    labels = issue.get("jira_labels", [])
    escalation = []
    if "Ceph_L3" in labels:
        escalation.append("L3 escalation")
    if "IBM_Customer_Issue" in labels:
        escalation.append("IBM customer impact")

    lines = [f"SUMMARY: {subject}"]
    lines.append("")
    lines.append("Issue Details:")
    lines.append(f"- JIRA: {jira_key}")
    lines.append(f"- Affected version(s): {versions}")
    lines.append(f"- Component(s): {', '.join(components) if components else 'Not specified'}")
    lines.append(f"- Severity: {severity}")
    if bz_ids:
        lines.append(f"- Related Bugzilla: {', '.join('BZ#' + b for b in bz_ids)}")
    if escalation:
        lines.append(f"- Escalation: {', '.join(escalation)}")

    if dev_hints:
        lines.append("")
        lines.append("Developer Analysis (from JIRA comments):")
        for hint in dev_hints:
            lines.append(f"- {hint}")

    if errors:
        lines.append("")
        lines.append("Key Error Indicators:")
        for e in errors:
            lines.append(f"- {e}")

    if comp_info.get("debug_cmds"):
        lines.append("")
        lines.append("Debug Commands:")
        for cmd in comp_info["debug_cmds"]:
            lines.append(f"- `{cmd}`")

    return "\n".join(lines)


def _classify_issue(subject: str) -> list[str]:
    """Classify issue type from subject line."""
    tags = []
    s = subject.lower()
    if re.search(r"upgrade|migration|backport", s):
        tags.append("upgrade")
    if re.search(r"crash|assert|segfault|core dump|abort|coredump", s):
        tags.append("crash")
    if re.search(r"ssl|cert|tls|certificate", s):
        tags.append("ssl")
    if re.search(r"500|error|fail|broken|not working|does not|unable to|cannot", s):
        tags.append("error")
    if re.search(r"slow|performance|latency|high.*cpu|memory.*leak|high.*memory", s):
        tags.append("perf")
    if re.search(r"permission|access control|denied|auth|unauthorized", s):
        tags.append("access")
    if re.search(r"mount|unmount|fuse|kernel client", s):
        tags.append("mount")
    if re.search(r"clone|snapshot|subvolume", s):
        tags.append("snapshot")
    if re.search(r"resharding|shard|bucket", s):
        tags.append("bucket")
    if re.search(r"sync|replicate|multisite|multizone", s):
        tags.append("sync")
    if re.search(r"grafana|prometheus|metric|alert|monitor", s):
        tags.append("monitoring")
    if re.search(r"osd.*down|osd.*fail|osd.*crash|pg.*inconsist|peering", s):
        tags.append("osd_failure")
    if re.search(r"mds.*crash|mds.*fail|standby|rank", s):
        tags.append("mds_failure")
    if re.search(r"nfs.*crash|nfs.*fail|ganesha|export", s):
        tags.append("nfs_failure")
    if re.search(r"hostname|dns|resolve|lower.?case", s):
        tags.append("hostname")
    if re.search(r"firewall|iptables|port|network", s):
        tags.append("network")
    if re.search(r"delete|remov|purg|cleanup", s):
        tags.append("delete")
    if re.search(r"rfe|feature|enhance|implement|add support", s):
        tags.append("rfe")
    if re.search(r"lvm|device|disk|inventory", s):
        tags.append("device")
    if re.search(r"ingress|haproxy|keepalived|vip|load.?balanc", s):
        tags.append("ingress")
    if re.search(r"daemon.*start|daemon.*fail|container.*fail|service.*fail", s):
        tags.append("daemon_start")
    return tags if tags else ["general"]


def _scenario_steps(comp: str, tags: list[str], subject: str, versions: str) -> list[str]:
    """Generate concrete reproduction steps based on component and issue tags."""
    steps = []
    step = 0

    def add(s):
        nonlocal step
        step += 1
        steps.append(f"{step}. {s}")

    # --- Cluster baseline ---
    add(f"Deploy IBM Ceph {versions} cluster: `cephadm bootstrap --mon-ip=<bootstrap-ip>`")
    add("Add hosts: `ceph orch host add <host> <ip>`")
    add("Add OSDs: `ceph orch apply osd --all-available-devices`")
    add("Verify cluster health: `ceph -s`")

    # --- Component-specific setup ---
    if comp in ("ceph-dashboard", "management & ui"):
        add("Enable dashboard: `ceph mgr module enable dashboard`")
        add("Set dashboard credentials: `ceph dashboard ac-user-create admin -i <pw-file> administrator`")
        add("Access dashboard: `curl -k https://<mgr-host>:8443/`")
        if "ssl" in tags:
            add("Prepare SSL cert and key files for the RGW/dashboard service")
            add("Attempt SSL cert upload via dashboard UI or API: `ceph dashboard set-ssl-certificate -i cert.pem`")
            add("Check mgr logs for errors: `cephadm shell -- ceph log last ceph-mgr 100`")
        elif "monitoring" in tags:
            add("Navigate to Grafana dashboards via dashboard: Cluster > Grafana")
            add("Verify Grafana datasource: `curl -k https://<grafana-host>:3000/api/datasources`")
            add("Check Prometheus targets: `curl http://<mgr-host>:9283/metrics | head -20`")
        elif "error" in tags:
            add("Navigate to the failing dashboard page/feature")
            add("Open browser dev tools, check Network tab for 500 responses")
            add("Check mgr module logs: `cephadm shell -- ceph log last ceph-mgr 100`")
        else:
            add("Navigate to the relevant dashboard section")
            add("Perform the operation described in the issue")
            add("Check mgr logs: `cephadm shell -- ceph log last ceph-mgr 100`")

    elif comp in ("rgw",):
        add("Deploy RGW service: `ceph orch apply rgw default --placement='2 <host1> <host2>'`")
        add("Wait for RGW daemons: `ceph orch ps --daemon-type rgw`")
        add("Create test user: `radosgw-admin user create --uid=testuser --display-name='Test User' --access-key=test --secret=test`")
        if "bucket" in tags or "resharding" in tags:
            add("Create bucket: `aws --endpoint-url=http://<rgw>:80 s3 mb s3://testbucket`")
            add("Upload objects to trigger resharding threshold: `for i in $(seq 1 200000); do echo $i | aws --endpoint-url=http://<rgw>:80 s3 cp - s3://testbucket/obj-$i; done`")
            add("Check resharding status: `radosgw-admin reshard status --bucket=testbucket`")
            add("Check bucket index: `radosgw-admin bucket stats --bucket=testbucket`")
        elif "crash" in tags:
            add("Run the workload/operation described in the issue against the RGW endpoint")
            add("Monitor for crashes: `ceph crash ls-new`")
            add("Collect crash info: `ceph crash info <crash-id>`")
            add("Check RGW logs: `cephadm shell -- ceph log last ceph-rgw 200`")
        elif "ssl" in tags:
            add("Configure RGW with SSL: `ceph config set rgw rgw_frontends 'beast port=443s ssl_certificate=<cert>'`")
            add("Verify SSL endpoint: `curl -k https://<rgw>:443/`")
        else:
            add("Perform S3 operation described in the issue")
            add("Check RGW logs: `cephadm shell -- ceph log last ceph-rgw 100`")

    elif comp == "rgw-multisite":
        add("Deploy primary zone RGW: `radosgw-admin realm create --rgw-realm=test --default`")
        add("Create zonegroup: `radosgw-admin zonegroup create --rgw-zonegroup=default --master --default`")
        add("Create primary zone: `radosgw-admin zone create --rgw-zone=zone1 --master --default`")
        add("Deploy secondary zone RGW on secondary cluster")
        add("Verify sync status: `radosgw-admin sync status`")
        if "sync" in tags:
            add("Create bucket and objects on primary: `aws --endpoint=http://<primary>:80 s3 cp testfile s3://testbucket/`")
            add("Verify sync on secondary: `aws --endpoint=http://<secondary>:80 s3 ls s3://testbucket/`")
            add("Check sync lag: `radosgw-admin data sync status --source-zone=zone1`")
        elif "delete" in tags:
            add("Delete objects/bucket on primary")
            add("Verify deletion propagates to secondary")
            add("Check sync markers: `radosgw-admin sync status`")
        else:
            add("Perform the multisite operation described in the issue")
            add("Check sync: `radosgw-admin sync status`")

    elif comp == "cephadm":
        if "upgrade" in tags:
            add("Set container image: `ceph config set mgr container_image <target-image>`")
            add("Start upgrade: `ceph orch upgrade start --image <target-image>`")
            add("Monitor upgrade: `ceph orch upgrade status`")
            add("Watch daemon versions: `ceph versions`")
            add("Verify all daemons upgraded: `ceph orch ps`")
        elif "hostname" in tags:
            add("Record current hostnames: `ceph orch host ls`")
            add("Perform the upgrade or operation that triggers hostname change")
            add("Compare hostnames: `ceph orch host ls` vs `hostname` on each node")
            add("Check for failed services: `ceph orch ps --format json | jq '.[] | select(.status_desc != \"running\")'`")
        elif "daemon_start" in tags:
            add("Check daemon status: `ceph orch ps --daemon-type=<type>`")
            add("Check systemd unit: `systemctl status ceph-<fsid>@<daemon>.service`")
            add("Check container logs: `podman logs <container-id>`")
            add("Check cephadm logs: `cephadm shell -- ceph log last cephadm 100`")
        else:
            add("Check orchestrator status: `ceph orch status`")
            add("List services: `ceph orch ls`")
            add("Perform the cephadm operation described in the issue")
            add("Check logs: `cephadm shell -- ceph log last cephadm 100`")

    elif comp == "rados":
        add("Create test pool: `ceph osd pool create testpool 32`")
        if "crash" in tags or "osd_failure" in tags:
            add("Run I/O workload: `rados bench -p testpool 60 write --no-cleanup`")
            add("Monitor OSD status: `ceph osd tree`")
            add("Check for crashes: `ceph crash ls-new`")
            add("Get crash details: `ceph crash info <crash-id>`")
            add("Check OSD logs: `cephadm shell -- ceph log last ceph-osd 200`")
            add("Check PG states: `ceph pg stat` and `ceph pg dump_stuck`")
        elif "perf" in tags:
            add("Baseline benchmark: `rados bench -p testpool 60 write --no-cleanup`")
            add("Read benchmark: `rados bench -p testpool 60 rand`")
            add("Check OSD perf: `ceph osd perf`")
            add("Check slow ops: `ceph daemon osd.<id> dump_ops_in_flight`")
        else:
            add("Run rados operations related to the issue scenario")
            add("Check PG health: `ceph pg stat`")
            add("Check OSD status: `ceph osd tree`")
            add("Check logs: `cephadm shell -- ceph log last ceph-osd 100`")

    elif comp == "cephfs":
        add("Create filesystem: `ceph fs volume create testfs`")
        add("Wait for MDS: `ceph mds stat`")
        add("Mount via kernel: `mount -t ceph <mon>:/ /mnt/cephfs -o name=admin,secret=<key>`")
        if "snapshot" in tags:
            add("Create subvolume: `ceph fs subvolume create testfs testvol`")
            add("Write test data to the subvolume mount path")
            add("Create snapshot: `ceph fs subvolume snapshot create testfs testvol snap1`")
            add("Attempt clone/cancel as described: `ceph fs subvolume snapshot clone testfs testvol snap1 clone1`")
            add("Check clone status: `ceph fs clone status testfs clone1`")
        elif "mount" in tags:
            add("Test mount: `mount -t ceph <mon>:/ /mnt/test -o name=admin`")
            add("Test FUSE mount: `ceph-fuse /mnt/fuse`")
            add("Run I/O: `dd if=/dev/urandom of=/mnt/cephfs/testfile bs=1M count=100`")
        elif "mds_failure" in tags:
            add("Check MDS ranks: `ceph fs status`")
            add("Identify active MDS: `ceph mds stat`")
            add("Trigger MDS failover: `ceph mds fail <rank>`")
            add("Verify standby promotion: `ceph mds stat`")
        else:
            add("Perform CephFS operations described in the issue")
            add("Check MDS status: `ceph fs status`")
            add("Check logs: `cephadm shell -- ceph log last ceph-mds 100`")

    elif comp in ("smb", "smb/samba"):
        add("Create CephFS: `ceph fs volume create smbfs`")
        add("Create SMB cluster: `ceph smb cluster create <name> user --define-user-pass=testuser%password`")
        add("Create share: `ceph smb share create <cluster> share1 smbfs / --share-name=share1`")
        add("Verify SMB service: `ceph orch ps --daemon-type smb`")
        if "access" in tags:
            add("Connect with AD user: `smbclient //<host>/share1 -U <domain>/<user>%<pass>`")
            add("Test file operations: `put testfile`, `get testfile`, `ls`")
            add("Check permissions: `smbcacls //<host>/share1 / -U <user>%<pass>`")
            add("Modify ACL via MMC or smbcacls and verify")
        elif "perf" in tags:
            add("Create many small files: `for i in $(seq 1 10000); do echo test > /mnt/smb/file-$i; done`")
            add("Measure sync time and compare with direct MDS mount")
            add("Check Samba logs for throttling or errors")
        else:
            add("Connect: `smbclient //<host>/share1 -U testuser%password`")
            add("Perform operations described in the issue")
            add("Check SMB logs: `cephadm shell -- ceph log last smbd 100`")

    elif comp in ("nfs", "nfs-ganesha", "nfs-ganesha/ceph"):
        add("Create NFS cluster: `ceph nfs cluster create nfs-test '<placement>'`")
        add("Create CephFS export: `ceph nfs export create cephfs nfs-test /export testfs /`")
        add("Verify export: `ceph nfs export ls nfs-test`")
        add("Mount on client: `mount -t nfs <nfs-host>:/export /mnt/nfs`")
        if "nfs_failure" in tags or "crash" in tags:
            add("Run I/O on mount: `fio --name=nfs-test --directory=/mnt/nfs --rw=randrw --bs=4k --size=100M --numjobs=4 --time_based --runtime=60`")
            add("Check NFS daemon status: `ceph orch ps --daemon-type nfs`")
            add("Check Ganesha logs: `cephadm shell -- ceph log last nfs 200`")
            add("Check for crashes: `ceph crash ls-new`")
        elif "ingress" in tags:
            add("Deploy ingress: `ceph orch apply ingress nfs.nfs-test --frontend-port=2049 --virtual-ip=<vip>/24`")
            add("Mount via VIP: `mount -t nfs <vip>:/export /mnt/nfs-vip`")
            add("Verify I/O: `dd if=/dev/urandom of=/mnt/nfs-vip/test bs=1M count=10`")
            add("Simulate failover: stop NFS daemon on active node")
            add("Verify client reconnects and I/O resumes")
        elif "daemon_start" in tags:
            add("Reboot the NFS host: `ssh <host> reboot`")
            add("Wait for host to come back: `ceph orch host ls`")
            add("Check NFS daemon recovery: `ceph orch ps --daemon-type nfs`")
            add("Verify mount still works: `ls /mnt/nfs/`")
        else:
            add("Run I/O: `dd if=/dev/urandom of=/mnt/nfs/test bs=1M count=50`")
            add("Perform the NFS operation described in the issue")
            add("Check logs: `cephadm shell -- ceph log last nfs 100`")

    elif comp == "rbd":
        add("Create RBD pool: `ceph osd pool create rbdpool 32`")
        add("Enable application: `ceph osd pool application enable rbdpool rbd`")
        add("Create image: `rbd create --size 1G rbdpool/testimg`")
        if "mount" in tags:
            add("Map image: `rbd map rbdpool/testimg`")
            add("Create filesystem: `mkfs.ext4 /dev/rbd0`")
            add("Mount: `mount /dev/rbd0 /mnt/rbd`")
            add("Run I/O: `dd if=/dev/urandom of=/mnt/rbd/test bs=1M count=100`")
        elif "delete" in tags:
            add("Attempt delete: `rbd rm rbdpool/testimg`")
            add("If protected, check: `rbd info rbdpool/testimg`")
            add("Check watchers: `rbd status rbdpool/testimg`")
            add("Force remove if needed: `rbd rm rbdpool/testimg --force`")
        else:
            add("Perform the RBD operation described in the issue")
            add("Check status: `rbd status rbdpool/testimg`")
            add("Check logs: `cephadm shell -- ceph log last ceph-osd 100`")

    elif comp in ("ceph-mgr/orchestrator", "ceph-mgr/crash", "ceph-mgr/core", "ceph-mgr/prometheus"):
        add("Check MGR status: `ceph mgr stat`")
        add("List modules: `ceph mgr module ls`")
        if "crash" in tags:
            add("Check crash list: `ceph crash ls-new`")
            add("Get crash info: `ceph crash info <crash-id>`")
            add("Check MGR logs: `cephadm shell -- ceph log last ceph-mgr 200`")
        elif "monitoring" in tags:
            add("Check Prometheus endpoint: `curl http://<mgr>:9283/metrics | head -50`")
            add("Verify Grafana datasource connection")
            add("Check alert rules: `ceph status`")
        else:
            add("Perform the MGR operation described in the issue")
            add("Check logs: `cephadm shell -- ceph log last ceph-mgr 100`")

    elif comp == "ceph-ansible":
        add("Setup ansible inventory for target hosts")
        if "network" in tags or "firewall" in tags:
            add("Check firewalld status on targets: `ssh <host> systemctl status firewalld`")
            add("Run preflight: `ansible-playbook cephadm-preflight.yml -i inventory`")
            add("Check for firewalld-related errors in output")
            add("Verify ports: `ssh <host> firewall-cmd --list-ports`")
        else:
            add("Run preflight: `ansible-playbook cephadm-preflight.yml -i inventory`")
            add("Deploy cluster: `cephadm bootstrap --mon-ip=<ip>`")
            add("Check deployment result: `ceph -s`")

    elif comp == "ceph-volume":
        add("List available devices: `ceph-volume inventory`")
        if "device" in tags:
            add("Check LVM status: `ceph-volume lvm list`")
            add("Create OSD: `ceph orch daemon add osd <host>:<device>`")
            add("Verify OSD: `ceph osd tree`")
        else:
            add("Run ceph-volume operation described in the issue")
            add("Check: `ceph-volume lvm list`")

    elif comp == "nvmeof":
        add("Deploy NVMe-oF gateway: `ceph orch apply nvmeof <pool> --placement='<host>'`")
        add("Create subsystem and namespace")
        add("Connect initiator: `nvme connect -t tcp -a <gw-ip> -s 4420 -n <nqn>`")
        add("Verify I/O to NVMe device: `dd if=/dev/urandom of=/dev/nvme0n1 bs=1M count=100`")

    else:
        add("Configure the component described in the issue")
        add("Perform the operation described in the JIRA summary")
        add("Check cluster health: `ceph health detail`")
        add("Check daemon status: `ceph orch ps`")
        add("Check logs: `cephadm shell -- ceph log last 100`")

    return steps


def build_repro_steps(issue: dict) -> str:
    components = issue.get("components", [])
    subject = issue.get("subject", "")
    versions = _get_versions(issue)
    primary_comp = components[0] if components else "unknown"
    comp_info = COMPONENT_INFO.get(primary_comp, {})
    tags = _classify_issue(subject)
    repro_hints = _extract_repro_hints(issue.get("timeline", []))

    lines = [f"SUMMARY: Reproduce {primary_comp} issue on IBM Ceph {versions}"]
    lines.append("")
    lines.append("Environment:")
    lines.append(f"- IBM Ceph version: {versions}")
    lines.append(f"- Component: {comp_info.get('area', primary_comp)}")
    if tags != ["general"]:
        lines.append(f"- Issue type: {', '.join(tags)}")

    lines.append("")
    lines.append("Workflow:")
    scenario = _scenario_steps(primary_comp, tags, subject, versions)
    lines.extend(scenario)

    if repro_hints:
        lines.append("")
        lines.append("Additional context from JIRA:")
        for hint in repro_hints:
            lines.append(f"- {hint}")

    lines.append("")
    lines.append("Validate:")
    lines.append("- Cluster health: `ceph health detail`")
    lines.append("- Daemon status: `ceph orch ps`")
    lines.append("- Crash dumps: `ceph crash ls-new`")
    if comp_info.get("log_cmd"):
        lines.append(f"- Logs: `{comp_info['log_cmd']}`")

    return "\n".join(lines)


def build_test_coverage(issue: dict) -> str:
    components = issue.get("components", [])
    subject = issue.get("subject", "")
    bz_ids = _get_bz_ids(issue)
    primary_comp = components[0] if components else "unknown"
    comp_info = COMPONENT_INFO.get(primary_comp, {})
    test_suite = comp_info.get("test_suite", f"qa/suites/{primary_comp}")

    is_upgrade = bool(re.search(r"upgrade|migration", subject, re.I))
    is_crash = bool(re.search(r"crash|assert|segfault|core dump|abort", subject, re.I))
    is_regression = bool(re.search(r"regression", subject, re.I))
    is_perf = bool(re.search(r"slow|performance|high.*cpu|memory|latency", subject, re.I))
    is_error = bool(re.search(r"500|error|fail|broken|not working", subject, re.I))

    lines = [f"SUMMARY: Test coverage assessment for {primary_comp} issue"]

    lines.append("")
    lines.append("Existing Coverage:")
    lines.append(f"- Component test suite: `{test_suite}`")
    if bz_ids:
        lines.append(f"- Related BZ fixes: {', '.join('BZ#' + b for b in bz_ids)} -- check if regression tests exist")
    lines.append(f"- Pending: cephci repo integration for automated coverage check")

    lines.append("")
    lines.append("Coverage Gaps:")
    if is_upgrade:
        lines.append("- Upgrade/migration path testing for the specific version transition")
        lines.append("- Pre/post upgrade validation checks")
    if is_crash:
        lines.append("- Crash/assert scenario needs dedicated negative test")
        lines.append("- Crash dump analysis in regression suite")
    if is_regression:
        lines.append("- Regression test for the re-introduced bug")
    if is_perf:
        lines.append("- Performance benchmark for the affected code path")
    if is_error:
        lines.append("- Error handling test for the specific failure scenario")
    if not any([is_upgrade, is_crash, is_regression, is_perf, is_error]):
        lines.append(f"- Functional test covering the reported {primary_comp} scenario")

    lines.append("")
    lines.append("Recommended Tests:")
    lines.append(f"- Add regression test to `{test_suite}`")
    lines.append(f"- Negative test: verify proper error handling")
    if is_upgrade:
        lines.append(f"- Upgrade test: validate across version boundary")
    lines.append(f"- Integration test: end-to-end with customer-like config")

    return "\n".join(lines)


def main():
    if not STATE_PATH.exists():
        print(f"ERROR: State file not found: {STATE_PATH}")
        sys.exit(1)

    with open(STATE_PATH) as f:
        data = json.load(f)

    issues = data.get("issues", [])
    print(f"Processing {len(issues)} issues...")

    tracking = TrackingDB()
    new_count = 0

    for issue in issues:
        jira_key = issue["jira_ids"][0] if issue.get("jira_ids") else None
        if not jira_key:
            continue

        analysis = build_analysis(issue)
        repro = build_repro_steps(issue)
        coverage = build_test_coverage(issue)

        severity = issue.get("severity", "normal")
        priority = PRIORITY_MAP.get(severity, "P2")

        tracking.set(jira_key, {
            "qa_status": "needs_analysis",
            "internal_priority": priority,
            "analysis": analysis,
            "repro_steps": repro,
            "test_coverage": coverage,
            "hotfix_status": "",
            "notes": f"Auto-analyzed from JIRA data. Components: {', '.join(issue.get('components', []))}",
        })
        new_count += 1

    tracking.save()
    total = len(tracking.all_tracked())
    print(f"Done: {new_count} issues analyzed")
    print(f"Total tracked issues: {total}")


if __name__ == "__main__":
    main()
