# Copyright 2026 gentoo-on-uconsole Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit meson

DESCRIPTION="Wayland logout menu"
HOMEPAGE="https://github.com/ArtsyMacaw/wlogout"
SRC_URI="https://github.com/ArtsyMacaw/${PN}/archive/refs/tags/${PV}.tar.gz -> ${P}.tar.gz"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64 ~arm64"
IUSE="bash-completion fish-completion man zsh-completion"

DEPEND="
	gui-libs/gtk-layer-shell
	x11-libs/gtk+:3[wayland]
"
RDEPEND="${DEPEND}"
BDEPEND="
	man? ( app-text/scdoc )
	virtual/pkgconfig
"

src_configure() {
	local emesonargs=(
		-Dbash-completions=$(usex bash-completion true false)
		-Dfish-completions=$(usex fish-completion true false)
		-Dman-pages=$(usex man enabled disabled)
		-Dzsh-completions=$(usex zsh-completion true false)
	)

	meson_src_configure
}
