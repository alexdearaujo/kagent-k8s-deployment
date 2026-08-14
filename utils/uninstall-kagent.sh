#!/bin/bash

# clean-up environment
helm uninstall kagent -n kentik
kubectl delete pvc -l app.kubernetes.io/name=kagent -n kentik
kubectl delete secret kagent-0-secret -n kentik
kubectl delete namespace kentik
rm -rf config-kagent/

kubectl delete all -l app=local-path-provisioner -n local-path-storage
kubectl delete namespace local-path-storage
kubectl delete storageclass local-path
kubectl delete clusterrole local-path-provisioner-role
kubectl delete clusterrolebinding local-path-provisioner-bind
