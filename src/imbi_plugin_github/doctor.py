"""GitHub-native analysis plugin (Project Doctor) for the GitHub plugin family.

Performs richer, GitHub-aware validation than the generic exists-in doctor:
identifier is an integer, canonical URL follows the /repositories/{id} shape
that the lifecycle plugin writes, dashboard link matches the API html_url, and
the legacy github-repository link matches as well.

Three concrete subclasses cover github.com, GHEC tenants, and GHES appliances,
following the same base/subclass/host-flavor pattern as every other plugin
family in this package.
"""

from __future__ import annotations

import abc
import re
import typing

import httpx
from imbi_common.plugins.base import (
    AnalysisPlugin,
    AnalysisResultItem,
    AnalysisResultStatus,
    CredentialField,
    PluginContext,
    PluginManifest,
    PluginOption,
    ServiceConnection,
)

from imbi_plugin_github._hosts import (
    host_to_api_base,
    normalize_host,
    require_ghec_tenant_host,
)
from imbi_plugin_github._repos import derive_owner_repo_from_links

_TIMEOUT = 15.0
# Matches the canonical URL shape written by the lifecycle plugin:
# https://api.{host}/repositories/{integer_id}
_REPO_ID_RE = re.compile(r'.*/repositories/(\d+)$')


def _item(
    slug: str,
    title: str,
    status: AnalysisResultStatus,
    description: str,
) -> AnalysisResultItem:
    return AnalysisResultItem(
        slug=slug,
        title=title,
        status=status,
        description=description,
    )


def _repo_fetch_url(
    connection: ServiceConnection,
    ctx: PluginContext,
    host: str,
    api_base: str,
) -> str | None:
    """Resolve the GitHub API URL for the project's repository.

    Prefer the rename-stable canonical URL on the ``EXISTS_IN`` edge;
    otherwise derive ``(owner, repo)`` from the project links. Returns
    ``None`` when neither is available.
    """
    if connection.canonical_url:
        return connection.canonical_url
    derived = derive_owner_repo_from_links(
        ctx.project_links,
        host,
        preferred_key=ctx.third_party_service_slug,
    )
    if derived is not None:
        owner, repo = derived
        return f'{api_base}/repos/{owner}/{repo}'
    return None


class _GitHubDoctorBase(AnalysisPlugin):
    """Abstract base for the GitHub doctor plugin family.

    Subclasses override ``_resolve_host`` to return the target GitHub host;
    all analysis logic lives here.
    """

    @classmethod
    @abc.abstractmethod
    def _resolve_host(cls, options: dict[str, typing.Any]) -> str: ...

    async def analyze(  # noqa: C901 — flat sequence of independent checks
        self,
        ctx: PluginContext,
        credentials: dict[str, str],
    ) -> list[AnalysisResultItem]:
        results: list[AnalysisResultItem] = []

        host = self._resolve_host(ctx.assignment_options)
        api_base = host_to_api_base(host)

        # Step 1: locate the EXISTS_IN connection for this service.
        slug = ctx.third_party_service_slug
        if not slug:
            return [
                _item(
                    'exists-in',
                    'Service binding',
                    'warn',
                    'This plugin is not bound to a third-party service — '
                    'no EXISTS_IN edge can be inspected.',
                )
            ]

        connection = next(
            (c for c in ctx.service_connections if c.service_slug == slug),
            None,
        )
        if connection is None:
            return [
                _item(
                    'exists-in',
                    'EXISTS_IN edge',
                    'warn',
                    f'No EXISTS_IN edge found for service {slug!r}. '
                    'Run the lifecycle plugin to create the repository '
                    'link and re-index this project.',
                )
            ]

        results.append(
            _item(
                'exists-in',
                'EXISTS_IN edge',
                'pass',
                f'EXISTS_IN edge for {slug!r} is present '
                f'(identifier={connection.identifier!r}).',
            )
        )

        # Step 2: build the Bearer token (optional).
        token = credentials.get('access_token') or credentials.get('token')
        headers: dict[str, str] = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        # Step 3: determine the URL to fetch.
        fetch_url = _repo_fetch_url(connection, ctx, host, api_base)
        if fetch_url is None:
            results.append(
                _item(
                    'canonical-url',
                    'Canonical URL',
                    'warn',
                    'No canonical URL on the EXISTS_IN edge and no '
                    'resolvable project link — cannot fetch the '
                    'GitHub repository.',
                )
            )
            # Without a URL we cannot run any body-dependent checks.
            results.extend(_body_unavailable_items())
            return results

        # Step 4: fetch the repo.
        body: dict[str, typing.Any] | None = None
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=_TIMEOUT,
            ) as client:
                resp = await client.get(fetch_url)
        except httpx.TransportError as exc:
            results.append(
                _item(
                    'canonical-url',
                    'Canonical URL',
                    'fail',
                    f'Transport error fetching {fetch_url!r}: {exc}',
                )
            )
            results.extend(_body_unavailable_items())
            return results

        if resp.status_code in (401, 403):
            if token:
                status: AnalysisResultStatus = 'fail'
                hint = (
                    'Token was present but rejected — check that the '
                    'access token has at minimum repo scope.'
                )
            else:
                status = 'warn'
                hint = (
                    'No access token configured; the repository may be '
                    'private.  Configure an access_token credential to '
                    'inspect private repositories.'
                )
            results.append(
                _item(
                    'canonical-url',
                    'Canonical URL',
                    status,
                    f'HTTP {resp.status_code} from {fetch_url!r}. {hint}',
                )
            )
            results.extend(_body_unavailable_items())
            return results

        if resp.status_code == 404:
            results.append(
                _item(
                    'canonical-url',
                    'Canonical URL',
                    'fail',
                    f'Repository not found at {fetch_url!r} (HTTP 404). '
                    'The repository may have been deleted or moved.',
                )
            )
            results.extend(_body_unavailable_items())
            return results

        if not resp.is_success:
            results.append(
                _item(
                    'canonical-url',
                    'Canonical URL',
                    'fail',
                    f'Unexpected HTTP {resp.status_code} from {fetch_url!r}.',
                )
            )
            results.extend(_body_unavailable_items())
            return results

        results.append(
            _item(
                'canonical-url',
                'Canonical URL',
                'pass',
                f'Fetched {fetch_url!r} — HTTP {resp.status_code}.',
            )
        )
        body = typing.cast(dict[str, typing.Any], resp.json())

        # Step 5: body-dependent checks.

        # identifier-type
        try:
            int(connection.identifier)
        except (ValueError, TypeError):
            results.append(
                _item(
                    'identifier-type',
                    'Identifier type',
                    'fail',
                    f'EXISTS_IN identifier {connection.identifier!r} cannot '
                    'be parsed as an integer. GitHub repository IDs are '
                    'always integers; the stored value is corrupt.',
                )
            )
        else:
            results.append(
                _item(
                    'identifier-type',
                    'Identifier type',
                    'pass',
                    f'EXISTS_IN identifier {connection.identifier!r} parses '
                    'as an integer.',
                )
            )

        # identifier-match
        api_id = str(body.get('id', ''))
        if api_id == connection.identifier:
            results.append(
                _item(
                    'identifier-match',
                    'Identifier match',
                    'pass',
                    f'EXISTS_IN identifier {connection.identifier!r} matches '
                    f'the GitHub API id {api_id!r}.',
                )
            )
        else:
            results.append(
                _item(
                    'identifier-match',
                    'Identifier match',
                    'fail',
                    f'EXISTS_IN identifier {connection.identifier!r} does not '
                    f'match the GitHub API id {api_id!r}. '
                    'Re-run the lifecycle plugin to repair the edge.',
                )
            )

        # canonical-url-shape
        if connection.canonical_url:
            expected_prefix = f'{api_base}/repositories/'
            match = _REPO_ID_RE.fullmatch(connection.canonical_url)
            if match and connection.canonical_url.startswith(expected_prefix):
                results.append(
                    _item(
                        'canonical-url-shape',
                        'Canonical URL shape',
                        'pass',
                        f'Canonical URL {connection.canonical_url!r} follows '
                        f'the https://api.{{host}}/repositories/{{id}} shape.',
                    )
                )
            else:
                results.append(
                    _item(
                        'canonical-url-shape',
                        'Canonical URL shape',
                        'fail',
                        f'Canonical URL {connection.canonical_url!r} does not '
                        f'follow the expected '
                        f'{api_base}/repositories/{{id}} shape. '
                        'Re-run the lifecycle plugin to repair the edge.',
                    )
                )
        else:
            results.append(
                _item(
                    'canonical-url-shape',
                    'Canonical URL shape',
                    'warn',
                    'No canonical URL stored on the EXISTS_IN edge — '
                    'shape cannot be verified.',
                )
            )

        # dashboard-url-match
        html_url = str(body.get('html_url', ''))
        tps_link = ctx.project_links.get(slug)
        if tps_link is None:
            results.append(
                _item(
                    'dashboard-url-match',
                    'Dashboard URL match',
                    'warn',
                    f'No dashboard link stored for service {slug!r}. '
                    'Run the lifecycle plugin to set the dashboard link.',
                )
            )
        elif tps_link == html_url:
            results.append(
                _item(
                    'dashboard-url-match',
                    'Dashboard URL match',
                    'pass',
                    f'Dashboard link {tps_link!r} matches the GitHub '
                    f'html_url {html_url!r}.',
                )
            )
        else:
            results.append(
                _item(
                    'dashboard-url-match',
                    'Dashboard URL match',
                    'fail',
                    f'Dashboard link {tps_link!r} does not match the GitHub '
                    f'html_url {html_url!r}. '
                    'Update the project link or re-run the lifecycle plugin.',
                )
            )

        # github-repository-link-match
        gh_link = ctx.project_links.get('github-repository')
        if gh_link is None:
            results.append(
                _item(
                    'github-repository-link-match',
                    'github-repository link match',
                    'warn',
                    'No github-repository link stored on the project. '
                    'Run the lifecycle plugin (or add the link manually) '
                    'to populate it.',
                )
            )
        elif gh_link == html_url:
            results.append(
                _item(
                    'github-repository-link-match',
                    'github-repository link match',
                    'pass',
                    f'github-repository link {gh_link!r} matches the '
                    f'GitHub html_url {html_url!r}.',
                )
            )
        else:
            results.append(
                _item(
                    'github-repository-link-match',
                    'github-repository link match',
                    'fail',
                    f'github-repository link {gh_link!r} does not match '
                    f'the GitHub html_url {html_url!r}. '
                    'Update the project link.',
                )
            )

        return results


def _body_unavailable_items() -> list[AnalysisResultItem]:
    """Return warn items for body-dependent checks when the fetch fails."""
    return [
        _item(
            'identifier-type',
            'Identifier type',
            'warn',
            'Cannot verify identifier type: repository fetch failed.',
        ),
        _item(
            'identifier-match',
            'Identifier match',
            'warn',
            'Cannot verify identifier: repository fetch failed.',
        ),
        _item(
            'canonical-url-shape',
            'Canonical URL shape',
            'warn',
            'Cannot verify canonical URL shape: repository fetch failed.',
        ),
        _item(
            'dashboard-url-match',
            'Dashboard URL match',
            'warn',
            'Cannot verify dashboard URL: repository fetch failed.',
        ),
        _item(
            'github-repository-link-match',
            'github-repository link match',
            'warn',
            'Cannot verify github-repository link: repository fetch failed.',
        ),
    ]


_COMMON_CREDENTIALS: list[CredentialField] = [
    CredentialField(
        name='access_token',
        label='Access token',
        description=(
            'Personal access token or server-side token with at minimum '
            'repo scope.  Optional — omit for public repositories.'
        ),
        required=False,
    ),
]


class GitHubDoctorPlugin(_GitHubDoctorBase):
    manifest = PluginManifest(
        slug='github-doctor',
        name='GitHub Doctor',
        description=(
            'Validates the EXISTS_IN edge for github.com: checks the '
            'identifier, canonical URL shape, dashboard link, and '
            'github-repository link against the live GitHub API.'
        ),
        plugin_type='analysis',
        credentials=_COMMON_CREDENTIALS,
    )

    @classmethod
    def _resolve_host(cls, options: dict[str, typing.Any]) -> str:
        del options
        return 'github.com'


class GitHubEnterpriseCloudDoctorPlugin(_GitHubDoctorBase):
    manifest = PluginManifest(
        slug='github-doctor-ec',
        name='GitHub Enterprise Cloud Doctor',
        description=(
            'Validates the EXISTS_IN edge for a GHEC tenant (*.ghe.com): '
            'checks the identifier, canonical URL shape, dashboard link, '
            'and github-repository link against the live GitHub Enterprise '
            'Cloud API.'
        ),
        plugin_type='analysis',
        options=[
            PluginOption(
                name='host',
                label='GHEC tenant host',
                description='e.g. tenant.ghe.com',
                type='string',
                required=True,
            ),
        ],
        credentials=_COMMON_CREDENTIALS,
    )

    @classmethod
    def _resolve_host(cls, options: dict[str, typing.Any]) -> str:
        return require_ghec_tenant_host(
            normalize_host(options.get('host'), 'GHEC doctor plugin'),
            'GHEC doctor plugin',
        )


class GitHubEnterpriseServerDoctorPlugin(_GitHubDoctorBase):
    manifest = PluginManifest(
        slug='github-doctor-es',
        name='GitHub Enterprise Server Doctor',
        description=(
            'Validates the EXISTS_IN edge for a GHES appliance: checks '
            'the identifier, canonical URL shape, dashboard link, and '
            'github-repository link against the live GitHub Enterprise '
            'Server API.'
        ),
        plugin_type='analysis',
        options=[
            PluginOption(
                name='host',
                label='GHES host',
                type='string',
                required=True,
            ),
        ],
        credentials=_COMMON_CREDENTIALS,
    )

    @classmethod
    def _resolve_host(cls, options: dict[str, typing.Any]) -> str:
        return normalize_host(options.get('host'), 'GHES doctor plugin')
