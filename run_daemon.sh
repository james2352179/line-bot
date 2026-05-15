#!/bin/bash
# KT BIKER Daemon 啟動腳本（launchd 用）
set -a
source "$(dirname "$0")/kt_biker.env"
# Supabase 憑證
source "$(dirname "$0")/kt_biker_supabase.env"
set +a
/opt/homebrew/bin/python3.12 "$(dirname "$0")/kt_biker_daemon.py"
