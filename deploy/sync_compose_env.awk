# Adds environment variables that the repo's compose file defines and the
# host's compose file lacks, for every service present in both.  Existing
# entries on the host are never changed or removed.
#
# usage: awk -v map="repo-service=host-service,..." -f sync_compose_env.awk \
#            REPO_COMPOSE HOST_COMPOSE HOST_COMPOSE > NEW_HOST_COMPOSE
#
# The host file is read twice: once to learn what it has, once to print it.
# Written for BusyBox awk (LibreELEC); no gawk extensions.

BEGIN {
    count = split(map, pairs, ",")
    for (i = 1; i <= count; i++) {
        if (split(pairs[i], kv, "=") == 2) {
            host_name[kv[1]] = kv[2]
        }
    }
}

FNR == 1 {
    fileno++
    in_services = 0
    svc = ""
    in_env = 0
    if (fileno == 3) {
        for (s in last_line) {
            insert_at[last_line[s]] = s
        }
    }
}

fileno == 3 {
    print
    if (FNR in insert_at) {
        s = insert_at[FNR]
        for (i = 1; i <= repo_count[s]; i++) {
            key = repo_key[s, i]
            if (!((s, key) in host_has)) {
                line = repo_line[s, key]
                sub(/^ +/, "", line)
                print key_indent[s] line
                print "[sync-env] " s ": " key " hinzugefuegt" > "/dev/stderr"
            }
        }
    }
    next
}

# Blank lines and comments never open or close a block
/^[ \t]*(#.*)?$/ { next }

{
    match($0, /^ */)
    indent = RLENGTH
}

indent == 0 {
    in_services = ($0 ~ /^services:/)
    svc = ""
    in_env = 0
    next
}

!in_services { next }

indent == 2 && /^  [A-Za-z0-9._-]+:[ \t]*(#.*)?$/ {
    svc = $1
    sub(/:.*/, "", svc)
    if (fileno == 1 && (svc in host_name)) {
        svc = host_name[svc]
    }
    if (fileno == 1) {
        repo_services[svc] = 1
    } else {
        host_services[svc] = 1
    }
    in_env = 0
    next
}

svc == "" { next }

in_env && indent <= env_indent { in_env = 0 }

!in_env && /^ +environment:[ \t]*(#.*)?$/ {
    in_env = 1
    env_indent = indent
    next
}

!in_env { next }

/^ +- / {
    if (fileno == 2) {
        list_form[svc] = 1
    }
    next
}

{
    if (fileno == 2) {
        last_line[svc] = FNR
    }
}

/^ +[A-Za-z_][A-Za-z0-9_]*[ \t]*:/ {
    key = $0
    sub(/^ +/, "", key)
    sub(/[ \t]*:.*/, "", key)
    if (fileno == 1) {
        repo_count[svc]++
        repo_key[svc, repo_count[svc]] = key
        repo_line[svc, key] = $0
    } else {
        host_has[svc, key] = 1
        if (!(svc in key_indent)) {
            key_indent[svc] = substr($0, 1, indent)
        }
    }
}

END {
    for (s in repo_count) {
        if (!(s in host_services)) {
            print "[sync-env] " s ": Service fehlt auf dem Host, uebersprungen" > "/dev/stderr"
        } else if (s in list_form) {
            print "[sync-env] " s ": environment ist eine Liste, uebersprungen" > "/dev/stderr"
        } else if (!(s in last_line)) {
            print "[sync-env] " s ": kein environment-Block auf dem Host, uebersprungen" > "/dev/stderr"
        }
    }
}
