"""Vendor-neutral deployment and external artifact adapter contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute


class KubernetesAdapter(Protocol):
    def verify_identity(self, *, observed_context: str, observed_cluster: str) -> None: ...

    def apply_chart(self, *, release: str, chart: str, version: str, namespace: str, dry_run: bool) -> list[str]: ...

    def remove_chart(self, *, release: str, namespace: str) -> list[str]: ...


@dataclass(frozen=True)
class HelmAdapter:
    context: str
    cluster: str
    namespace: str
    kubeconfig: str = ""

    def verify_identity(self, *, observed_context: str, observed_cluster: str) -> None:
        if observed_context != self.context:
            raise ValueError(f"Kubernetes 当前 context 不匹配: expected={self.context}, actual={observed_context}")
        if observed_cluster != self.cluster:
            raise ValueError(f"Kubernetes cluster identity 不匹配: expected={self.cluster}, actual={observed_cluster}")

    def apply_chart(self, *, release: str, chart: str, version: str, namespace: str, dry_run: bool) -> list[str]:
        if namespace != self.namespace:
            raise ValueError(f"Kubernetes namespace 不匹配: expected={self.namespace}, actual={namespace}")
        command = [
            "helm",
            "upgrade",
            "--install",
            release,
            chart,
            "--version",
            version,
            "--namespace",
            namespace,
            "--kube-context",
            self.context,
            "--atomic",
            "--wait",
        ]
        if self.kubeconfig:
            command.extend(("--kubeconfig", self.kubeconfig))
        if dry_run:
            command.append("--dry-run")
        return command

    def remove_chart(self, *, release: str, namespace: str) -> list[str]:
        if namespace != self.namespace:
            raise ValueError(f"Kubernetes namespace 不匹配: expected={self.namespace}, actual={namespace}")
        command = [
            "helm",
            "uninstall",
            release,
            "--ignore-not-found",
            "--namespace",
            namespace,
            "--kube-context",
            self.context,
        ]
        if self.kubeconfig:
            command.extend(("--kubeconfig", self.kubeconfig))
        return command


class ArtifactStore(Protocol):
    def publish(self, source: Path, immutable_key: str) -> dict[str, str]: ...

    def publish_channel(self, source: Path, channel: str) -> dict[str, str]: ...


@dataclass(frozen=True)
class FileArtifactStore:
    root: Path

    def publish(self, source: Path, immutable_key: str) -> dict[str, str]:
        if "latest" in immutable_key.lower():
            raise ValueError("client-package 禁止 latest 路径")
        payload = source.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        target = self.root / immutable_key
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
            raise FileExistsError(f"不可变制品已存在且内容不同: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        return {"ref": str(target), "sha256": sha256}

    def publish_channel(self, source: Path, channel: str) -> dict[str, str]:
        payload = source.read_bytes()
        target = self.root / "channels" / f"{channel}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)
        return {"ref": str(target), "sha256": hashlib.sha256(payload).hexdigest()}


@dataclass(frozen=True)
class HttpArtifactStore:
    endpoint: str
    token_env: str = ""

    def publish(self, source: Path, immutable_key: str) -> dict[str, str]:
        if "latest" in immutable_key.lower():
            raise ValueError("client-package 禁止 latest 路径")
        payload = source.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        url = f"{self.endpoint.rstrip('/')}/{immutable_key.lstrip('/')}"
        headers = {"Content-Type": "application/octet-stream", "X-Content-SHA256": sha256}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValueError(f"缺少 Artifact Store token 环境变量: {self.token_env}")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - endpoint is explicit project config
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Artifact Store PUT 失败: HTTP {response.status}")
        return {"ref": url, "sha256": sha256}

    def publish_channel(self, source: Path, channel: str) -> dict[str, str]:
        return self._put(source, f"channels/{channel}.json")

    def _put(self, source: Path, key: str) -> dict[str, str]:
        payload = source.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        url = f"{self.endpoint.rstrip('/')}/{key.lstrip('/')}"
        headers = {"Content-Type": "application/octet-stream", "X-Content-SHA256": sha256}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValueError(f"缺少 Artifact Store token 环境变量: {self.token_env}")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Artifact Store PUT 失败: HTTP {response.status}")
        return {"ref": url, "sha256": sha256}


def publish_client_bundle(
    store: ArtifactStore,
    *,
    package: Path,
    signature: Path,
    sbom: Path,
    immutable_key: str,
    channel: str = "",
) -> dict[str, object]:
    result: dict[str, object] = {
        "package": store.publish(package, immutable_key),
        "signature": store.publish(signature, immutable_key + ".sig"),
        "sbom": store.publish(sbom, immutable_key + ".sbom.json"),
    }
    if channel:
        manifest = package.parent / f".{package.name}.{channel}.json"
        manifest.write_text(
            json.dumps({"version_key": immutable_key, **result}, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            result["channel"] = store.publish_channel(manifest, channel)
        finally:
            manifest.unlink(missing_ok=True)
    return result


def execute_helm(command: list[str], *, cwd: Path) -> None:
    result = execute(CommandSpec.argv_command(command, cwd=cwd))
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or "Helm 执行失败")
