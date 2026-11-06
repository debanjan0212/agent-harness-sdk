{{- define "ic.labels" -}}
app.kubernetes.io/name: incident-commander
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
{{- define "ic.selector" -}}
app.kubernetes.io/name: incident-commander
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
