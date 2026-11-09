"""Read visible product regions only; never export page HTML or browser globals."""

SNAPSHOT = r"""() => {
  const visible = e => !!(e && e.getClientRects().length);
  const text = e => visible(e) ? (e.innerText || e.textContent || '').trim() : '';
  const first = selectors => {
    for (const selector of selectors) {
      const e = [...document.querySelectorAll(selector)].find(visible);
      if (text(e)) return text(e);
    }
    return '';
  };
  const nodes = selector => [...document.querySelectorAll(selector)].filter(visible);
  const source = e => e?.getAttribute?.('data-src') || e?.currentSrc || e?.src || '';
  const attributeNodes = nodes('[class*="Attrs"], [class*="attrs"], [class*="AttrInfo"], [class*="attrInfo"], [class*="BasicContent"]');
  const attributes = attributeNodes.flatMap(e => text(e).split('\n')).map(x => {
    const m = x.match(/^([^：:]{1,60})[：:]\s*(.+)$/);
    return m ? {name:m[1].trim(),value:m[2].trim()} : null;
  }).filter(Boolean);
  const detailNodes = nodes('[class*="descV8"], #J_DivItemDesc, [class*="detailContent"], [class*="DetailContent"], [class*="description"], [class*="Description"]');
  const detailImages = detailNodes.flatMap(e => [...e.querySelectorAll('img')]);
  const galleryImages = nodes('[class*="PicGallery"] img, [class*="picGallery"] img, [class*="gallery"] img, [class*="Gallery"] img, #J_UlThumb img');
  const materialPattern = /面料|材质|成分|棉|涤纶|氨纶|锦纶|粘纤|cotton|sorona|polyester/i;
  const materialClaims = [...attributes.filter(x=> materialPattern.test(x.name+' '+x.value)).map(x=>x.name+': '+x.value),
    ...detailNodes.flatMap(e=>text(e).split('\n')).filter(x=>materialPattern.test(x))];
  const priceNodes = nodes('[class*="Price"], [class*="price"]');
  const qualifications = priceNodes.map(text).filter(x=>x.length<500 && /券|起|预售|定金|到手|优惠|活动|会员|补贴|满减|coupon|from|deposit/i.test(x));
  const variants = nodes('[class*="skuItem--"], [class*="SkuItem--"], [class*="skuItem "]').map(e=>({
    name: text(e.querySelector('[class*="label"], [class*="Label"], [class*="title"], dt')),
    options: [...e.querySelectorAll('[class*="valueItem"], [class*="ValueItem"], [role="option"], li')].map(text).filter(Boolean)
  })).filter(x=>x.name && x.options.length);
  const shop = nodes('[class*="ShopInfo"] a,[class*="shopInfo"] a,[class*="ShopName"] a,[class*="shopName"] a,#J_ShopInfo a').find(e=> /taobao\.com|tmall\.com/.test(e.href) && /shop|store/i.test(e.className+' '+e.href));
  return {
    title: first(['[class*="mainTitle"]','[class*="MainTitle"]','h1','#J_Title h3']),
    price_text: first(['[class*="highlightPrice"]','[class*="HighlightPrice"]','[class*="priceText"]','#J_PromoPriceNum','#J_StrPriceModBox .tb-rmb-num']),
    price_qualifications: qualifications, variants, attributes,
    seller: shop ? {name:text(shop),url:shop.href} : {},
    gallery: galleryImages.map(source), detail_images:detailImages.map(source), material_claims:materialClaims,
    size_chart_images: detailImages.filter(e=>/尺码|尺寸|size|measurement/i.test(e.alt||'')).map(source),
    links: nodes('a[href*="item.taobao.com"],a[href*="pcdetail.taobao.com"],a[href*="detail.tmall.com"]').map(a=>({
      url:a.href, title: a.getAttribute('title') || text(a.querySelector('[class*="title"], [class*="Title"]')) || text(a),
      price_text: text(a.querySelector('[class*="price"], [class*="Price"]')),
      image: source(a.querySelector('img') || {})
    }))
  };
}"""
