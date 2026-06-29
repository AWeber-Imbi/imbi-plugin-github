"""Tests for the GitHub-native project-doctor analysis plugin."""

import unittest

import httpx
import respx
from imbi_common.plugins.base import (
    PluginContext,
    ServiceConnection,
    ServicePlugin,
)
from imbi_common.plugins.errors import (
    PluginAuthenticationFailed,
    PluginRemediationNotSupported,
)

from imbi_plugin_github.doctor import (
    _REPAIR_EDGE,
    _REPAIR_GITHUB_LINK,
    GitHubDoctorPlugin,
)

_HOST = 'aweber.ghe.com'
_API_BASE = 'https://api.aweber.ghe.com'
_CANONICAL = f'{_API_BASE}/repositories/134741'
_DASHBOARD = f'https://{_HOST}/aweber/demo'
_REPO_PAYLOAD = {'id': 134741, 'html_url': _DASHBOARD, 'name': 'demo'}

_TPS_SLUG = 'aweber-github'
_CREDS = {'access_token': 'gho_test'}


def _connection() -> ServicePlugin:
    """The github-connection sibling the doctor resolves its host from."""
    return ServicePlugin(
        slug='github-connection',
        options={'flavor': 'ghec', 'host': _HOST},
    )


def _ctx(
    *,
    service_slug: str | None = _TPS_SLUG,
    connections: list[ServiceConnection] | None = None,
    links: dict[str, str] | None = None,
    service_plugins: list[ServicePlugin] | None = None,
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
        service_plugins=(
            service_plugins if service_plugins is not None else [_connection()]
        ),
    )


def _by_slug(items: object) -> dict[str, object]:
    return {i.slug: i for i in items}  # type: ignore[attr-defined]


class ManifestTestCase(unittest.TestCase):
    def test_manifest(self) -> None:
        manifest = GitHubDoctorPlugin.manifest
        self.assertEqual(manifest.slug, 'github-doctor')
        self.assertEqual(manifest.plugin_type, 'analysis')

    def test_declares_no_options_or_credentials(self) -> None:
        # Host and shared App/PAT credentials come from the
        # github-connection plugin, not the doctor's own assignment.
        manifest = GitHubDoctorPlugin.manifest
        self.assertEqual(manifest.options, [])
        self.assertEqual(manifest.credentials, [])


class AnalyzeTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_warns_without_service_binding(self) -> None:
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(service_slug=None), {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].slug, 'exists-in')
        self.assertEqual(results[0].status, 'warn')

    async def test_warns_when_no_connection(self) -> None:
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(connections=[]), {})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].slug, 'exists-in')
        self.assertEqual(results[0].status, 'warn')

    async def test_warns_when_no_github_connection_plugin(self) -> None:
        # Without a github-connection sibling the host cannot be resolved.
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(service_plugins=[]), _CREDS)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].slug, 'connection')
        self.assertEqual(results[0].status, 'warn')

    @respx.mock
    async def test_happy_path(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(_by_slug(results)['identifier-match'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_canonical_url_wrong_shape(self) -> None:
        # Canonical URL uses /repos/owner/repo instead of /repositories/{id}
        bad_canonical = f'{_API_BASE}/repos/aweber/demo'
        respx.get(bad_canonical).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
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
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(
            _by_slug(results)['github-repository-link-match'].status,
            'pass',  # type: ignore[attr-defined]
        )

    @respx.mock
    async def test_canonical_fetch_fails_401_no_token(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(401))
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), {})
        by = _by_slug(results)
        self.assertEqual(by['canonical-url'].status, 'warn')  # type: ignore[attr-defined]
        # Body-dependent checks should all be warn when fetch fails
        self.assertEqual(by['identifier-type'].status, 'warn')  # type: ignore[attr-defined]
        self.assertEqual(by['identifier-match'].status, 'warn')  # type: ignore[attr-defined]

    @respx.mock
    async def test_canonical_fetch_fails_401_with_token(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(401))
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(_by_slug(results)['canonical-url'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_canonical_fetch_404(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(404))
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        by = _by_slug(results)
        self.assertEqual(by['canonical-url'].status, 'fail')  # type: ignore[attr-defined]
        self.assertEqual(by['identifier-match'].status, 'warn')  # type: ignore[attr-defined]

    @respx.mock
    async def test_transport_error(self) -> None:
        respx.get(_CANONICAL).mock(side_effect=httpx.ConnectError('boom'))
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        self.assertEqual(_by_slug(results)['canonical-url'].status, 'fail')  # type: ignore[attr-defined]

    @respx.mock
    async def test_derives_url_from_links_when_no_canonical_url(self) -> None:
        derived_url = f'{_API_BASE}/repos/aweber/demo'
        respx.get(derived_url).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
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


class RemediationOfferTestCase(unittest.IsolatedAsyncioTestCase):
    @respx.mock
    async def test_fail_findings_carry_offers(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json={**_REPO_PAYLOAD, 'id': 999})
        )
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        by = _by_slug(results)
        offer = by['identifier-match'].remediation  # type: ignore[attr-defined]
        self.assertIsNotNone(offer)
        self.assertEqual(offer.id, _REPAIR_EDGE)

    @respx.mock
    async def test_passing_findings_have_no_offer(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
        results = await plugin.analyze(_ctx(), _CREDS)
        for item in results:
            self.assertIsNone(item.remediation)  # type: ignore[attr-defined]


class RemediateTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_id_raises(self) -> None:
        plugin = GitHubDoctorPlugin()
        with self.assertRaises(PluginRemediationNotSupported):
            await plugin.remediate(_ctx(), _CREDS, 'bogus')

    async def test_no_connection_failed(self) -> None:
        plugin = GitHubDoctorPlugin()
        result = await plugin.remediate(
            _ctx(connections=[]), _CREDS, _REPAIR_EDGE
        )
        self.assertEqual(result.status, 'failed')

    @respx.mock
    async def test_edge_repair_emits_service_writeback(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json={**_REPO_PAYLOAD, 'id': 999})
        )
        plugin = GitHubDoctorPlugin()
        ctx = _ctx(
            connections=[
                ServiceConnection(
                    service_slug=_TPS_SLUG,
                    identifier='134741',
                    canonical_url=_CANONICAL,
                )
            ]
        )
        result = await plugin.remediate(ctx, _CREDS, _REPAIR_EDGE)
        self.assertEqual(result.status, 'fixed')
        self.assertIsNotNone(ctx.service_writeback)
        assert ctx.service_writeback is not None
        self.assertEqual(ctx.service_writeback.identifier, '999')
        self.assertEqual(
            ctx.service_writeback.canonical_url,
            f'{_API_BASE}/repositories/999',
        )
        self.assertEqual(
            ctx.service_writeback.dashboard_links, {_TPS_SLUG: _DASHBOARD}
        )

    @respx.mock
    async def test_edge_repair_noop_when_correct(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
        ctx = _ctx()
        result = await plugin.remediate(ctx, _CREDS, _REPAIR_EDGE)
        self.assertEqual(result.status, 'noop')
        self.assertIsNone(ctx.service_writeback)

    @respx.mock
    async def test_github_link_repair_emits_link_writeback(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
        ctx = _ctx(
            links={
                _TPS_SLUG: _DASHBOARD,
                'github-repository': f'https://{_HOST}/aweber/stale',
            }
        )
        result = await plugin.remediate(ctx, _CREDS, _REPAIR_GITHUB_LINK)
        self.assertEqual(result.status, 'fixed')
        assert ctx.link_writeback is not None
        self.assertEqual(ctx.link_writeback.link_key, 'github-repository')
        self.assertEqual(ctx.link_writeback.new_url, _DASHBOARD)

    @respx.mock
    async def test_github_link_repair_noop_when_correct(self) -> None:
        respx.get(_CANONICAL).mock(
            return_value=httpx.Response(200, json=_REPO_PAYLOAD)
        )
        plugin = GitHubDoctorPlugin()
        ctx = _ctx()
        result = await plugin.remediate(ctx, _CREDS, _REPAIR_GITHUB_LINK)
        self.assertEqual(result.status, 'noop')
        self.assertIsNone(ctx.link_writeback)

    @respx.mock
    async def test_401_propagates_for_identity_retry(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(401))
        plugin = GitHubDoctorPlugin()
        with self.assertRaises(PluginAuthenticationFailed):
            await plugin.remediate(_ctx(), _CREDS, _REPAIR_EDGE)

    @respx.mock
    async def test_non_success_failed(self) -> None:
        respx.get(_CANONICAL).mock(return_value=httpx.Response(404))
        plugin = GitHubDoctorPlugin()
        result = await plugin.remediate(_ctx(), _CREDS, _REPAIR_EDGE)
        self.assertEqual(result.status, 'failed')


if __name__ == '__main__':
    unittest.main()
