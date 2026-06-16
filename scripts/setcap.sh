#!/bin/sh
set -ex

target_bin=$1

sudo setcap cap_sys_nice+ep "${target_bin}"
