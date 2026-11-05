# headless-shell 只帶 Chrome 本身，沒有中文字型：繁中網頁會整片豆腐。這一層只加字型，
# 其他照原 image。base 與字型版本的正本是 config/versions.env；Compose 傳入固定值。
# 理由見 docs/adr/0014-add-cjk-fonts-to-the-chrome-sidecar.md。
ARG CHROME_IMAGE
FROM ${CHROME_IMAGE}

ARG FONTS_NOTO_CJK_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends "fonts-noto-cjk=${FONTS_NOTO_CJK_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && test -e /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
