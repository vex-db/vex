{{/*
Chart name / fullname helpers.
*/}}
{{- define "vex-bench.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vex-bench.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "vex-bench.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "vex-bench.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "vex-bench.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels for a given server key (passed via dict: root + serverName).
*/}}
{{- define "vex-bench.serverSelectorLabels" -}}
app.kubernetes.io/name: {{ include "vex-bench.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .serverName }}
{{- end -}}

{{/*
ServiceAccount name used by the orchestrator Job.
*/}}
{{- define "vex-bench.serviceAccountName" -}}
{{- printf "%s-runner" (include "vex-bench.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
ConfigMap name that holds the results CSV (when writeConfigMap is enabled).
*/}}
{{- define "vex-bench.resultsConfigMapName" -}}
{{- printf "%s-results" (include "vex-bench.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
