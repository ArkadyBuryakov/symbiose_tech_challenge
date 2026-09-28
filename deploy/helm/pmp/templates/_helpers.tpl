{{- define "pmp.image" -}}
{{ .Values.registry }}/{{ .name }}:{{ .Values.tag }}
{{- end }}

{{- define "pmp.commonEnv" -}}
- {name: ENVIRONMENT, value: prod}
- {name: LOG_LEVEL, value: INFO}
- {name: LOG_FORMAT, value: json}
- {name: GIT_SHA, value: {{ .Values.tag | quote }}}
- {name: PUBLIC_BASE_URL, value: {{ .Values.publicBaseUrl | quote }}}
- {name: AWS_REGION, value: {{ .Values.region | quote }}}
# boto3 reads only this one; the Node SDK reads AWS_REGION.
- {name: AWS_DEFAULT_REGION, value: {{ .Values.region | quote }}}
- {name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "http://otel-collector:4317"}
{{- end }}

{{- define "pmp.dbEnv" -}}
- {name: DB_HOST, value: {{ .Values.db.host | quote }}}
- {name: DB_PORT, value: "5432"}
- {name: DB_NAME, value: {{ .Values.db.name | quote }}}
- {name: DB_AUTH, value: {{ .auth | default "iam" }}}
- {name: DB_SSLMODE, value: verify-full}
- {name: PGSSLMODE, value: verify-full}
- {name: PGSSLROOTCERT, value: /etc/rds/ca.pem}
{{- end }}

{{/* The pmp-kafka chart's broker, fully qualified for KEDA. */}}
{{- define "pmp.kafkaBootstrap" -}}
kafka.{{ .Release.Namespace }}.svc.cluster.local:9092
{{- end }}

{{- define "pmp.kafkaEnv" -}}
- {name: KAFKA_BOOTSTRAP_SERVERS, value: {{ include "pmp.kafkaBootstrap" . | quote }}}
{{- end }}

{{- define "pmp.s3Env" -}}
- {name: S3_REGION, value: {{ .Values.region | quote }}}
- {name: S3_STAGING_BUCKET, value: {{ .Values.s3.stagingBucket | quote }}}
- {name: S3_PUBLISH_BUCKET, value: {{ .Values.s3.publishBucket | quote }}}
{{- end }}

{{/* Secrets Manager -> /run/keys, the same paths as dev-keys/ locally. */}}
{{- define "pmp.keysVolume" -}}
- name: keys
  csi:
    driver: secrets-store.csi.k8s.io
    readOnly: true
    volumeAttributes: {secretProviderClass: {{ . }}}
{{- end }}

{{- define "pmp.rdsCaVolume" -}}
- name: rds-ca
  configMap: {name: rds-ca}
{{- end }}

{{- define "pmp.securityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities: {drop: [ALL]}
{{- end }}

{{/* Created before the hook Jobs, which need them, and on every upgrade. */}}
{{- define "pmp.preHook" -}}
annotations:
  helm.sh/hook: pre-install,pre-upgrade
  helm.sh/hook-weight: "-10"
  helm.sh/hook-delete-policy: before-hook-creation
{{- end }}
