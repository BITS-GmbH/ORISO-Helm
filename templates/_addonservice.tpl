{{- define "oriso.addonService.image" -}}
{{- $image := dig "image" (dict) .Values.addonService -}}
{{- $repository := required "addonService.image.repository must be set when addonService.enabled is true" (get $image "repository" | default "") -}}
{{- $tag := required "addonService.image.tag must be set when addonService.enabled is true" (get $image "tag" | default "") -}}
{{- if hasPrefix "sha256:" $tag -}}
{{- printf "%s@%s" $repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "oriso.addonService.dbUsername" -}}
{{- dig "secrets" "addonServiceDbUsername" "addonservice" .Values.global -}}
{{- end -}}

{{- define "oriso.addonService.dbPassword" -}}
{{- required "global.secrets.addonServiceDbPassword is required when addonService.enabled is true" (dig "secrets" "addonServiceDbPassword" "" .Values.global) -}}
{{- end -}}

{{- define "oriso.addonService.tokenPrivateKeyPem" -}}
{{- $pem := required "addonService.tokenPrivateKeyPem is required when addonService.enabled is true (PKCS#8 RSA key, see README)" (dig "tokenPrivateKeyPem" "" .Values.addonService) -}}
{{- if not (contains "-----BEGIN PRIVATE KEY-----" $pem) -}}
{{- fail "addonService.tokenPrivateKeyPem must be a PKCS#8 PEM (-----BEGIN PRIVATE KEY-----)" -}}
{{- end -}}
{{- $pem -}}
{{- end -}}

{{- define "oriso.addonService.discoveryNamespace" -}}
{{- dig "discovery" "namespace" "" .Values.addonService | default "addons" -}}
{{- end -}}

{{- define "oriso.addonService.kubernetesDiscovery" -}}
{{- dig "discovery" "kubernetes" true .Values.addonService | toString -}}
{{- end -}}
