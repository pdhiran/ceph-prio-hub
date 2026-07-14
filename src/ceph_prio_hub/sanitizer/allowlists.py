"""Allowlists for the sanitizer — domains, IPs, and hostnames to keep.

Ported from ceph-issue-kb/src/ceph_issue_kb/indexer/sanitizer.py with
additions for prio-list workflow (wwpdl.vnet.ibm.com, etc.).
"""

ALLOWED_DOMAINS = frozenset({
    "redhat.com", "ibm.com", "ceph.io", "ceph.com",
    "suse.com", "suse.de", "github.com", "github.io",
    "bugzilla.redhat.com", "access.redhat.com", "tracker.ceph.com",
    "atlassian.net", "atlassian.com", "googleapis.com",
    "quay.io", "registry.redhat.io", "openshift.com",
    "openssh.com", "libssh.org", "kernel.org", "gnu.org",
    "openssl.org", "fedoraproject.org", "centos.org", "ubuntu.com",
    "debian.org", "python.org", "golang.org", "apache.org",
    "linuxfoundation.org",
    "example.com", "example.org", "example.net",
    "lists.sourceforge.net", "lists.podman.io",
    "sourceforge.net", "nongnu.org",
    "wwpdl.vnet.ibm.com",
    "vnet.ibm.com",
})

ALLOWED_IP_PREFIXES = (
    "127.", "0.0.0.0", "255.255.255.",
    "10.0.0.", "10.0.1.", "10.0.2.",
    "192.168.0.", "192.168.1.",
)

ALLOWED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain",
    "localhost4", "localhost4.localdomain4",
    "localhost6", "localhost6.localdomain6",
})


def is_allowed_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d.endswith(a) or d.endswith("." + a) for a in ALLOWED_DOMAINS)


def is_allowed_ip(ip: str) -> bool:
    return any(ip.startswith(p) for p in ALLOWED_IP_PREFIXES)


def is_allowed_hostname(hostname: str) -> bool:
    return hostname.lower().split(".")[0] in ALLOWED_HOSTNAMES
