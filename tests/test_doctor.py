"""Tests for the GitHub-native project-doctor analysis plugin."""

import unittest

import httpx
import respx
from imbi_common.plugins.base import PluginContext, ServiceConnection

from imbi_plugin_github.doctor import (
    GitHubDoctorPlugin,
    GitHubEnterpriseCloudDoctorPlugin,
    GitHubEnterpriseServerDoctorPlugin,
)

_HOST = 'aweber.ghe.com'
_API_BASE = 'https://api.aweber.ghe.com'
_CANONICAL = f'{_API_BASE}/repositories/134741'
_DASHBOARD = f'https://{_HOST}/aweber/demo'
_REPO_PAYLOAD = {'id': 134741, 'html_url': _DASHBOARD, 'name': 'demo'}

_TPS_SLUG = 'aweber-github'
_CREDS = {'access_token': 'gho_test'}


def _ctx(
    *,
    service_slug: str | None = _TPS_SLUG,
    connections: list[ServiceConnection] | None = None,
    links: dict[str, str] | None = None,
    options: dict[str, object] | None = None,
) -> PluginContext:
    if connections is None:
        connections = [
            ServiceConnection(
                service_slug=_TPS_SLUG,
                identifier='134741',
                canonical_url=_CANONICAL,
            )
        ]
    default_links: dict[str, str] = {
        _TPS_SLUG: _DASHBOARD,
        'github-repository': _DASHBOARD,
    }
    return PluginContext(
        project_id='p',
        project_slug='demo',
        org_slug='aweber',
        third_party_service_slug=service_slug,
        service_connections=connections,
        project_links=links if links is not None else default_links,
        assignment_options=options or {'host': _HOST},
    )


def _by_slug(items: object) -> dict[str, object]:
    return {i.slug: i for i in items}  # type: ignore[attr-defined]


class ManifestTestCase(unittest.TestCase):
    def test_manifest(self) -> None:
        manifest = GitHubEnterpriseCloudDoctorPlugin.manifest
        self.assertEqual(manifest.slug, 'github-doctor-ec')
        self.assertEqual(manifest.plugin_type, 'analysis')
        self.assertFalse(manifest.credentials[0].required)

    def test_ghes_manifest(self) -> None:
        manifest = GitHubEnterpriseServerDoctorPlugin.manifest
        self.assertEqual(manifest.slug, 'github-doctor-es')
        self.assertEqual(manifest.plugin_type, 'analysis')

    def test_github_com_manifest(self) -> None:
        manifest = GitHubDoctorPlugin.manifest
        self.assertEqual(manifest.slug, 'github-doctor')
        self.assertEqual(manifest.plugin_type, 'analysis')


class AnalyzeTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_warns_without_service_binding(self) -> None:
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(service_slug=None), {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].slug, 'exists-in')
        self.assertEqual(results[0].status, 'warn')

    async def test_warns_when_no_connection(self) -> None:
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(connections=[]), {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].slug, 'exists-in')
        self.assertEqual(results[0].status, 'warn')

    @respx.mock
    async def test_happy_path(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        by = _by_slug(results)
        self.assertEqual(len(results), 7)
        self.assertEqual(by['exists-in'].status, 'pass')  # type: ignore[attr-defined]
        self.assertEqual(by['canonical-url'].status, 'pass')  # type: ignore[attr-defined]
        self.assertEqual(by['identifier-type'].status, 'pass')  # type: ignore[attr-defined]
        self.assertEqual(by['identifier-match'].status, 'pass')  # type: ignore[attr-defined]
        self.assertEqual(by['canonical-url-shape'].status, 'pass')  # type: ignore[attr-defined]
        self.assertEqual(by['dashboard-url-match'].status, 'pass')  # type: ignore[attr-defined]
        self.assertEqual(by['github-repository-link-match'].status, 'pass')  # type: ignore[attr-defined]

    @respx.mock
    async def test_identifier_not_integer(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(
                connections=[
                    ServiceConnection(
                        service_slug=_TPS_SLUG,
                        identifier='abc',
                        canonical_url=_CANONICAL,
                    )
                ]
            ),
            _CREDS,
        )
        self.assertEqual(_by_slug(results)['identifier-type'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_identifier_mismatch(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json={**_REPO_PAYLOAD, 'id': 999})
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(_by_slug(results)['identifier-match'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_canonical_url_wrong_shape(self) -> None:
        # Canonical URL uses /repos/owner/repo instead of /repositories/{id}
        bad_canonical = f'{_API_BASE}/repos/aweber/demo'
        respx.get(bad_canonical).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(
                connections=[
                    ServiceConnection(
                        service_slug=_TPS_SLUG,
                        identifier='134741',
                        canonical_url=bad_canonical,
                    )
                ]
            ),
            _CREDS,
        )
        self.assertEqual(
            _by_slug(results)['canonical-url-shape'].status,
            'fail',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_canonical_url_wrong_host(self) -> None:
        # Canonical URL points at api.github.com instead of api.aweber.ghe.com
        wrong_host_canonical = 'https://api.github.com/repositories/134741'
        respx.get(wrong_host_canonical).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(
                connections=[
                    ServiceConnection(
                        service_slug=_TPS_SLUG,
                        identifier='134741',
                        canonical_url=wrong_host_canonical,
                    )
                ]
            ),
            _CREDS,
        )
        self.assertEqual(
            _by_slug(results)['canonical-url-shape'].status,
            'fail',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_dashboard_url_missing(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        # No TPS-slug key in project_links
        results = await plugin.analyze(
            _ctx(links={'github-repository': _DASHBOARD}),
            _CREDS,
        )
        self.assertEqual(
            _by_slug(results)['dashboard-url-match'].status,
            'warn',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_dashboard_url_mismatch(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(
                links={
                    _TPS_SLUG: f'https://{_HOST}/aweber/other',
                    'github-repository': _DASHBOARD,
                }
            ),
            _CREDS,
        )
        self.assertEqual(
            _by_slug(results)['dashboard-url-match'].status,
            'fail',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_github_repository_link_missing(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(links={_TPS_SLUG: _DASHBOARD}),
            _CREDS,
        )
        self.assertEqual(
            _by_slug(results)['github-repository-link-match'].status,
            'warn',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_github_repository_link_mismatch(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(
                links={
                    _TPS_SLUG: _DASHBOARD,
                    'github-repository': f'https://{_HOST}/aweber/other',
                }
            ),
            _CREDS,
        )
        self.assertEqual(
            _by_slug(results)['github-repository-link-match'].status,
            'fail',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_github_repository_link_matches(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(
            _by_slug(results)['github-repository-link-match'].status,
            'pass',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_canonical_fetch_fails_401_no_token(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(401))
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), {})
        by = _by_slug(results)
        self.assertEqual(by['canonical-url'].status, 'warn')  # type: ignore[attr-defined]
        # Body-dependent checks should all be warn when fetch fails
        self.assertEqual(by['identifier-type'].status, 'warn')  # type: ignore[attr-defined]
        self.assertEqual(by['identifier-match'].status, 'warn')  # type: ignore[attr-defined]

    @respx.mock
    async def test_canonical_fetch_fails_401_with_token(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(401))
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(_by_slug(results)['canonical-url'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_canonical_fetch_404(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(404))
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        by = _by_slug(results)
        self.assertEqual(by['canonical-url'].status, 'fail')  # type: ignore[attr-defined]
        self.assertEqual(by['identifier-match'].status, 'warn')  # type: ignore[attr-defined]

    @respx.mock
    async def test_transport_error(self) -> None:
        respx.get(_CANONICAL).mock(side_effect=httpx.ConnectError('boom'))
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(_by_slug(results)['canonical-url'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_derives_url_from_links_when_no_canonical_url(self) -> None:
        derived_url = f'{_API_BASE}/repos/aweber/demo'
        respx.get(derived_url).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubEnterpriseCloudDoctorPlugin()
        results = await plugin.analyze(
            _ctx(
                connections=[
                    ServiceConnection(
                        service_slug=_TPS_SLUG,
                        identifier='134741',
                        canonical_url=None,
                    )
                ],
            ),
            _CREDS,
        )
        by = _by_slug(results)
        self.assertEqual(by['canonical-url'].status, 'pass')  # type: ignore[attr-defined]
        # No canonical URL on the edge → shape check should warn
        self.assertEqual(by['canonical-url-shape'].status, 'warn')  # type: ignore[attr-defined]


if __name__ == '__main__':
    unittest.main()
