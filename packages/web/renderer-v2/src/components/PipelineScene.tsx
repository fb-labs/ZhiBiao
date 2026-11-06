import { useEffect, useRef } from 'react';
import * as THREE from 'three';

type Props = {
  dense?: boolean;
};

type LabelSpec = {
  title: string;
  caption: string;
  accent: string;
};

const STATION_LABELS: Record<string, LabelSpec> = {
  docs: { title: '01 招标文件', caption: '条款 · 格式 · 评分办法', accent: '#6366f1' },
  analysis: { title: '02 AI 解读', caption: '资格 · 评分 · 风险', accent: '#22d3ee' },
  outline: { title: '03 标书章节', caption: '结构 · 篇幅 · 逐章生成', accent: '#8b5cf6' },
  delivery: { title: '04 交付文件', caption: 'DOCX · PDF · 核验', accent: '#0d9488' },
};

function roundRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  context.beginPath();
  context.moveTo(x + radius, y);
  context.lineTo(x + width - radius, y);
  context.quadraticCurveTo(x + width, y, x + width, y + radius);
  context.lineTo(x + width, y + height - radius);
  context.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  context.lineTo(x + radius, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - radius);
  context.lineTo(x, y + radius);
  context.quadraticCurveTo(x, y, x + radius, y);
  context.closePath();
}

function makeLabel(spec: LabelSpec): THREE.Sprite | null {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 136;
  const context = canvas.getContext('2d');
  if (!context) return null;

  roundRect(context, 2, 2, 508, 132, 26);
  context.fillStyle = 'rgba(255, 255, 255, 0.88)';
  context.fill();
  context.strokeStyle = 'rgba(99, 102, 241, 0.28)';
  context.lineWidth = 2;
  context.stroke();

  context.fillStyle = spec.accent;
  context.fillRect(2, 2, 8, 132);

  context.fillStyle = '#1e1b4b';
  context.font = '600 42px "PingFang SC", "Microsoft YaHei", sans-serif';
  context.fillText(spec.title, 30, 56);

  context.fillStyle = 'rgba(99, 102, 241, 0.78)';
  context.font = '24px "PingFang SC", "Microsoft YaHei", sans-serif';
  context.fillText(spec.caption, 30, 100);

  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, opacity: 0.92 });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(3.7, 1.0, 1);
  return sprite;
}

export function PipelineScene({ dense = false }: Props) {
  const host = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = host.current;
    if (!container) return;

    const compact = dense || window.matchMedia('(max-width: 720px)').matches;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const width = container.clientWidth || 800;
    const height = container.clientHeight || 360;
    const fov = 42;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(fov, width / height, 0.1, 140);
    // 按场景实际占位反推镜头距离：站点与标签横跨约 25.5，纵向内容约 8.6。
    // 比原始固定 20 稍近，但保留卡片四周留白，不把场景顶满。
    const fitCameraZ = (aspect: number) => {
      const halfTan = Math.tan(THREE.MathUtils.degToRad(fov) / 2);
      return Math.max(
        compact ? 21 : 16,
        8.6 / (2 * halfTan),
        25.5 / (2 * halfTan * Math.max(0.8, aspect)),
      );
    };
    const baseCamera = new THREE.Vector3(0, compact ? 1.2 : 1.4, fitCameraZ(width / height));
    camera.position.copy(baseCamera);
    camera.lookAt(0, 0.6, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
    renderer.setSize(width, height);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    container.appendChild(renderer.domElement);

    const group = new THREE.Group();
    group.position.set(compact ? 0 : dense ? 2.4 : 0, -0.1, 0);
    group.scale.setScalar(compact ? 0.74 : dense ? 0.92 : 1.2);
    scene.add(group);

    const colors = {
      indigo: 0x6366f1,
      violet: 0x8b5cf6,
      cyan: 0x22d3ee,
      teal: 0x0d9488,
      paper: 0xe9e6ff,
    };

    const lineMaterial = (color: number, opacity: number) =>
      new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
    const fillMaterial = (color: number, opacity: number) =>
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
    const glowMaterial = (color: number, opacity: number) =>
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });

    const stations: Record<string, THREE.Group> = {};
    const interactiveGroups: THREE.Group[] = [];

    const registerStation = (key: string, station: THREE.Group, haloRadius: number) => {
      station.userData.station = key;
      station.userData.hoverScale = 1;
      station.userData.pulse = 0;
      const halo = new THREE.Mesh(
        new THREE.TorusGeometry(haloRadius, 0.018, 8, 96),
        glowMaterial(colors.cyan, 0),
      );
      halo.rotation.x = Math.PI / 2;
      halo.position.y = -1.85;
      halo.position.z = 0.1;
      station.add(halo);
      station.userData.halo = halo;
      stations[key] = station;
      interactiveGroups.push(station);
      group.add(station);
    };

    // ── 01 招标文件：纸张、条款与格式骨架 ──
    const docs = new THREE.Group();
    docs.position.set(-6.9, -0.1, 0);
    docs.rotation.set(-0.08, 0.38, 0.02);
    const sheetGeo = new THREE.PlaneGeometry(3.1, 4.0);
    const sheetEdges = new THREE.EdgesGeometry(sheetGeo);
    for (let index = 0; index < 4; index += 1) {
      const sheet = new THREE.Mesh(
        sheetGeo,
        fillMaterial(index === 3 ? 0xe9e6ff : 0xdedaff, index === 3 ? 0.74 : 0.46),
      );
      sheet.position.set(index * 0.24, index * 0.16, -index * 0.06);
      sheet.rotation.set(0, 0, index * 0.02);
      docs.add(sheet);
      const edges = new THREE.LineSegments(sheetEdges, lineMaterial(colors.indigo, 0.65));
      edges.position.copy(sheet.position);
      edges.rotation.copy(sheet.rotation);
      docs.add(edges);
    }
    for (let line = 0; line < 7; line += 1) {
      const rule = new THREE.Mesh(
        new THREE.PlaneGeometry(line === 0 ? 1.3 : 2.1, 0.035),
        fillMaterial(colors.indigo, line === 0 ? 0.5 : 0.22),
      );
      rule.position.set(0.18, 1.35 - line * 0.38, 0.22);
      rule.rotation.z = 0.02;
      docs.add(rule);
    }
    for (let chip = 0; chip < 3; chip += 1) {
      const chipMesh = new THREE.Mesh(
        new THREE.PlaneGeometry(0.9 + chip * 0.22, 0.14),
        fillMaterial(colors.cyan, 0.32),
      );
      chipMesh.position.set(-0.8, 0.7 - chip * 0.34, 0.28);
      docs.add(chipMesh);
    }
    registerStation('docs', docs, 2.1);

    // ── 02 AI 解读：评分雷达、资格节点与量化柱 ──
    const analysis = new THREE.Group();
    analysis.position.set(-2.5, 0.25, 0.25);
    const ringOuter = new THREE.Mesh(
      new THREE.TorusGeometry(1.55, 0.03, 8, 140),
      lineMaterial(colors.cyan, 0.72),
    );
    ringOuter.rotation.set(Math.PI / 2.35, 0.15, 0.25);
    analysis.add(ringOuter);
    const ringInner = new THREE.Mesh(
      new THREE.TorusGeometry(1.05, 0.022, 8, 100),
      lineMaterial(colors.violet, 0.58),
    );
    ringInner.rotation.set(Math.PI / 2.1, -0.3, 0.1);
    analysis.add(ringInner);
    const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.72, 0), lineMaterial(colors.indigo, 0.5));
    analysis.add(core);
    const nodeGeo = new THREE.SphereGeometry(0.065, 12, 12);
    const scoreNodes: THREE.Mesh[] = [];
    for (let index = 0; index < 9; index += 1) {
      const angle = (index / 9) * Math.PI * 2;
      const node = new THREE.Mesh(nodeGeo, fillMaterial(index % 2 === 0 ? colors.indigo : colors.cyan, 0.85));
      node.position.set(Math.cos(angle) * 1.55, Math.sin(angle) * 0.55, Math.sin(angle) * 1.2);
      analysis.add(node);
      scoreNodes.push(node);
    }
    const scoreBars: { mesh: THREE.Mesh; phase: number }[] = [];
    for (let index = 0; index < 5; index += 1) {
      const barHeight = 0.38 + index * 0.18;
      const bar = new THREE.Mesh(
        new THREE.BoxGeometry(0.07, barHeight, 0.035),
        fillMaterial(index === 4 ? colors.cyan : colors.indigo, 0.58),
      );
      bar.position.set(1.92 + index * 0.19, -1.38 + barHeight / 2, 0.16);
      analysis.add(bar);
      scoreBars.push({ mesh: bar, phase: index * 0.45 });
    }
    registerStation('analysis', analysis, 2.0);

    // ── 03 标书章节：逐格点亮的写作网格 ──
    const outline = new THREE.Group();
    outline.position.set(2.55, 0.35, 0.1);
    outline.rotation.set(-0.1, -0.28, 0);
    const cellFills: { mesh: THREE.Mesh; phase: number }[] = [];
    for (let row = 0; row < 3; row += 1) {
      for (let column = 0; column < 3; column += 1) {
        const cellGeo = new THREE.PlaneGeometry(0.92, 0.56);
        const cell = new THREE.Mesh(cellGeo, fillMaterial(0xffffff, 0.58));
        cell.position.set((column - 1) * 1.04, (row - 1) * 0.72, 0);
        outline.add(cell);
        const fill = new THREE.Mesh(
          new THREE.PlaneGeometry(0.78, 0.42),
          glowMaterial(row === 1 && column === 1 ? colors.violet : colors.cyan, 0.16),
        );
        fill.position.set(cell.position.x, cell.position.y, 0.012);
        outline.add(fill);
        cellFills.push({ mesh: fill, phase: (row * 3 + column) * 0.16 });
        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(cellGeo),
          lineMaterial(row === 1 && column === 1 ? colors.violet : colors.indigo, 0.62),
        );
        edges.position.copy(cell.position);
        outline.add(edges);
        if (column < 2) {
          const link = new THREE.Mesh(new THREE.PlaneGeometry(0.14, 0.014), fillMaterial(colors.indigo, 0.3));
          link.position.set((column - 0.5) * 1.04, (row - 1) * 0.72, 0.01);
          outline.add(link);
        }
      }
    }
    registerStation('outline', outline, 2.0);

    // ── 04 交付文件：成稿、核验印章与验收勾 ──
    const delivery = new THREE.Group();
    delivery.position.set(6.8, -0.15, 0.2);
    delivery.rotation.set(-0.06, -0.4, 0.02);
    const finalSheetGeo = new THREE.PlaneGeometry(2.7, 3.55);
    const finalSheet = new THREE.Mesh(finalSheetGeo, fillMaterial(0xece9ff, 0.78));
    delivery.add(finalSheet);
    delivery.add(new THREE.LineSegments(new THREE.EdgesGeometry(finalSheetGeo), lineMaterial(colors.violet, 0.72)));
    for (let line = 0; line < 6; line += 1) {
      const rule = new THREE.Mesh(
        new THREE.PlaneGeometry(line === 0 ? 1.0 : 1.7, 0.03),
        fillMaterial(colors.indigo, line === 0 ? 0.45 : 0.18),
      );
      rule.position.set(0.2, 1.25 - line * 0.36, 0.01);
      delivery.add(rule);
    }
    const seal = new THREE.Mesh(new THREE.TorusGeometry(0.42, 0.03, 8, 60), lineMaterial(colors.teal, 0.68));
    seal.position.set(0.78, -1.05, 0.02);
    delivery.add(seal);
    const sealGlow = new THREE.Mesh(
      new THREE.TorusGeometry(0.46, 0.02, 8, 72),
      glowMaterial(colors.teal, 0.22),
    );
    sealGlow.position.copy(seal.position);
    delivery.add(sealGlow);
    const check = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0.55, -1.0, 0.03),
      new THREE.Vector3(0.74, -1.2, 0.03),
      new THREE.Vector3(1.02, -0.83, 0.03),
    ]);
    delivery.add(new THREE.Line(check, lineMaterial(colors.teal, 0.9)));
    registerStation('delivery', delivery, 1.9);

    // ── 站点标签：让三维场景与“招标解读—编写—检查—交付”主题直接对应 ──
    const labels: THREE.Sprite[] = [];
    if (!compact) {
      Object.entries(STATION_LABELS).forEach(([key, spec]) => {
        const sprite = makeLabel(spec);
        if (!sprite) return;
        const station = stations[key];
        sprite.position.set(0, 2.65, 0.1);
        station.add(sprite);
        labels.push(sprite);
      });
    }

    // ── 基线、站点刻度与环绕评分卫星 ──
    const baseline = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-9.1, -1.45, 0.35),
      new THREE.Vector3(9.1, -1.45, 0.35),
    ]);
    group.add(new THREE.Line(baseline, lineMaterial(colors.indigo, 0.22)));
    for (const stationX of [-6.9, -2.5, 2.55, 6.8]) {
      const tick = new THREE.Mesh(new THREE.SphereGeometry(0.045, 10, 10), fillMaterial(colors.indigo, 0.58));
      tick.position.set(stationX, -1.45, 0.36);
      group.add(tick);
    }

    const orbiters: THREE.Mesh[] = [];
    for (let index = 0; index < (compact ? 4 : 7); index += 1) {
      const orbiter = new THREE.Mesh(
        new THREE.SphereGeometry(0.055, 10, 10),
        glowMaterial(index % 2 === 0 ? colors.cyan : colors.violet, 0.7),
      );
      group.add(orbiter);
      orbiters.push(orbiter);
    }

    // ── 数据流：从文件流向交付 ──
    const flow = new THREE.CatmullRomCurve3([
      new THREE.Vector3(-5.1, 0.15, 0.7),
      new THREE.Vector3(-2.5, 0.7, 0.4),
      new THREE.Vector3(0.1, 1.05, 0.15),
      new THREE.Vector3(2.6, 0.75, 0.3),
      new THREE.Vector3(5.0, 0.2, 0.7),
    ]);
    group.add(new THREE.Mesh(new THREE.TubeGeometry(flow, 120, 0.012, 4, false), lineMaterial(colors.indigo, 0.24)));

    const flowCount = compact ? 32 : 52;
    const positions = new Float32Array(flowCount * 3);
    const tint = new Float32Array(flowCount * 3);
    const offsets = new Float32Array(flowCount);
    const cyanColor = new THREE.Color(colors.cyan);
    const violetColor = new THREE.Color(colors.violet);
    for (let index = 0; index < flowCount; index += 1) {
      offsets[index] = index / flowCount;
      const color = cyanColor.clone().lerp(violetColor, index / flowCount);
      tint[index * 3] = color.r;
      tint[index * 3 + 1] = color.g;
      tint[index * 3 + 2] = color.b;
    }
    const flowGeo = new THREE.BufferGeometry();
    flowGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    flowGeo.setAttribute('color', new THREE.BufferAttribute(tint, 3));
    const flowPoints = new THREE.Points(
      flowGeo,
      new THREE.PointsMaterial({
        size: compact ? 0.08 : 0.095,
        vertexColors: true,
        transparent: true,
        opacity: 0.78,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        sizeAttenuation: true,
      }),
    );
    group.add(flowPoints);

    // ── 扫描光带：横向掠过，强化“逐条核对”主题 ──
    const scan = new THREE.Mesh(new THREE.PlaneGeometry(0.055, 5.4), glowMaterial(colors.cyan, 0.32));
    scan.position.set(-8, 0.35, 1.15);
    group.add(scan);

    // ── 环境粒子 ──
    const dustCount = compact ? 28 : 70;
    const dustPositions = new Float32Array(dustCount * 3);
    for (let index = 0; index < dustCount; index += 1) {
      dustPositions[index * 3] = (Math.random() - 0.5) * 22;
      dustPositions[index * 3 + 1] = (Math.random() - 0.5) * 10;
      dustPositions[index * 3 + 2] = (Math.random() - 0.5) * 8;
    }
    const dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPositions, 3));
    const dust = new THREE.Points(
      dustGeo,
      new THREE.PointsMaterial({
        size: 0.05,
        color: colors.indigo,
        transparent: true,
        opacity: 0.22,
        depthWrite: false,
        sizeAttenuation: true,
      }),
    );
    scene.add(dust);

    // ── 鼠标交互：悬浮高亮、光标光点、点击涟漪 ──
    const cursorOrb = new THREE.Mesh(new THREE.SphereGeometry(0.11, 14, 14), glowMaterial(colors.cyan, 0.8));
    cursorOrb.visible = false;
    if (!compact) scene.add(cursorOrb);

    const trail: THREE.Mesh[] = [];
    if (!compact) {
      for (let index = 0; index < 6; index += 1) {
        const dot = new THREE.Mesh(
          new THREE.SphereGeometry(0.035 - index * 0.004, 8, 8),
          glowMaterial(index % 2 === 0 ? colors.cyan : colors.violet, 0.5 - index * 0.06),
        );
        dot.visible = false;
        scene.add(dot);
        trail.push(dot);
      }
    }

    const pointer = { x: 0, y: 0, active: false };
    // canvas 自身 pointer-events: none，事件要挂在有交互的父层（hero-visual），
    // 否则悬浮高亮、光标光点和点击涟漪永远不会触发。
    const surface = container.parentElement || container;
    const cursorTarget = new THREE.Vector3(0, 0, 1.5);
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    const hitPoint = new THREE.Vector3();
    let hovered = '';

    const updatePointer = (event: PointerEvent) => {
      const rect = surface.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      pointer.x = (px - 0.5) * 2;
      pointer.y = (py - 0.5) * 2;
      pointer.active = true;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      cursorTarget.set(pointer.x * 15, -pointer.y * 8, 1.5);

      raycaster.setFromCamera(ndc, camera);
      const hits = raycaster.intersectObjects(interactiveGroups, true);
      let found = '';
      for (const item of hits) {
        let current: THREE.Object3D | null = item.object;
        while (current && !current.userData.station) current = current.parent;
        if (current?.userData.station) {
          found = String(current.userData.station);
          break;
        }
      }
      hovered = found;
      surface.style.cursor = hovered ? 'pointer' : 'crosshair';
    };

    const onPointerLeave = () => {
      pointer.active = false;
      hovered = '';
      cursorOrb.visible = false;
      trail.forEach((dot) => {
        dot.visible = false;
      });
      surface.style.cursor = 'default';
    };

    type Ripple = { mesh: THREE.Mesh; material: THREE.MeshBasicMaterial; life: number };
    const ripples: Ripple[] = [];

    const onPointerDown = (event: PointerEvent) => {
      if (reduced) return;
      const rect = surface.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      raycaster.setFromCamera(ndc, camera);
      if (!raycaster.ray.intersectPlane(plane, hitPoint)) return;

      if (hovered && stations[hovered]) stations[hovered].userData.pulse = 1;

      if (ripples.length >= 4) return;
      const material = glowMaterial(colors.cyan, 0.75);
      const mesh = new THREE.Mesh(new THREE.TorusGeometry(0.22, 0.014, 8, 64), material);
      const localPoint = group.worldToLocal(hitPoint.clone());
      mesh.position.set(localPoint.x, localPoint.y, localPoint.z + 0.15);
      group.add(mesh);
      ripples.push({ mesh, material, life: 0 });
    };

    surface.addEventListener('pointermove', updatePointer);
    surface.addEventListener('pointerleave', onPointerLeave);
    surface.addEventListener('pointerdown', onPointerDown);

    let visible = true;
    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries[0]?.isIntersecting ?? true;
      },
      { threshold: 0.08 },
    );
    observer.observe(container);

    const clock = new THREE.Clock();
    let frame = 0;
    let running = true;

    const applyStatic = () => {
      docs.position.y = -0.1;
      analysis.rotation.y = 0.2;
      outline.position.y = 0.35;
      delivery.position.y = -0.15;
      renderer.render(scene, camera);
    };

    const render = () => {
      if (!running) return;
      if (!reduced && visible && !document.hidden) {
        const delta = Math.min(0.06, clock.getDelta());
        const time = clock.elapsedTime;

        camera.position.x += (baseCamera.x + pointer.x * (compact ? 0.28 : 0.9) - camera.position.x) * 0.08;
        camera.position.y += (baseCamera.y - pointer.y * (compact ? 0.18 : 0.55) - camera.position.y) * 0.08;
        camera.lookAt(0, 0.6, 0);

        docs.position.y = -0.1 + Math.sin(time * 0.9) * 0.06;
        docs.rotation.y = 0.38 + Math.sin(time * 0.36) * 0.1;
        analysis.rotation.y = 0.2 + Math.sin(time * 0.4) * 0.2;
        ringOuter.rotation.z = time * 0.12;
        ringInner.rotation.z = -time * 0.18;
        core.rotation.y = time * 0.35;
        core.rotation.x = time * 0.2;
        scoreNodes.forEach((node, index) => {
          const scale = 1 + Math.sin(time * 1.1 + index * 0.5) * 0.16;
          node.scale.setScalar(scale);
        });
        scoreBars.forEach(({ mesh, phase }) => {
          mesh.scale.y = 1 + Math.sin(time * 1.25 + phase) * 0.2;
        });
        outline.position.y = 0.35 + Math.sin(time * 0.7 + 1.2) * 0.05;
        cellFills.forEach(({ mesh, phase }) => {
          const wave = (Math.sin(time * 1.15 + phase) + 1) / 2;
          const material = mesh.material as THREE.MeshBasicMaterial;
          material.opacity = 0.1 + wave * 0.5;
        });
        delivery.position.y = -0.15 + Math.sin(time * 0.8 + 2.1) * 0.07;
        seal.rotation.z = time * 0.35;
        sealGlow.scale.setScalar(1 + Math.sin(time * 1.6) * 0.12);
        const sealMaterial = sealGlow.material as THREE.MeshBasicMaterial;
        sealMaterial.opacity = 0.16 + (Math.sin(time * 1.6) + 1) * 0.12;

        const attr = flowGeo.getAttribute('position') as THREE.BufferAttribute;
        const speed = 0.055 + Math.abs(pointer.x) * 0.025;
        for (let index = 0; index < flowCount; index += 1) {
          const progress = (offsets[index] + time * speed) % 1;
          const point = flow.getPointAt(progress);
          attr.setXYZ(index, point.x, point.y, point.z);
        }
        attr.needsUpdate = true;

        scan.position.x = -8 + ((time * 0.32) % 1) * 16;
        const scanMaterial = scan.material as THREE.MeshBasicMaterial;
        scanMaterial.opacity = 0.12 + (Math.sin(time * 1.4) + 1) * 0.16;

        orbiters.forEach((orbiter, index) => {
          const angle = time * 0.25 + (index / orbiters.length) * Math.PI * 2;
          const radius = 10.2 + (index % 3) * 0.5;
          orbiter.position.set(Math.cos(angle) * radius, Math.sin(angle * 1.4) * 2.2 + 0.4, Math.sin(angle) * 3.2);
        });

        dust.rotation.y += delta * 0.04;

        for (const [key, station] of Object.entries(stations)) {
          const target = key === hovered ? 1.07 : 1;
          const current = Number(station.userData.hoverScale || 1);
          station.userData.hoverScale = current + (target - current) * 0.1;
          const pulse = Number(station.userData.pulse || 0);
          station.scale.setScalar(station.userData.hoverScale + pulse * 0.05);
          station.userData.pulse = Math.max(0, pulse - delta * 2.4);
          const halo = station.userData.halo as THREE.Mesh | undefined;
          if (halo) {
            const material = halo.material as THREE.MeshBasicMaterial;
            const haloOpacity = key === hovered ? 0.38 : 0;
            material.opacity += (haloOpacity - material.opacity) * 0.12;
          }
        }

        if (!compact && (cursorOrb.visible || pointer.active)) {
          cursorOrb.visible = true;
          cursorOrb.position.lerp(cursorTarget, 0.16);
          cursorOrb.position.z = 1.4 + Math.sin(time * 2.4) * 0.08;
        }
        if (!compact) {
          let previous = cursorOrb.position.clone();
          trail.forEach((dot, index) => {
            if (!pointer.active) return;
            dot.visible = true;
            dot.position.lerp(previous, 0.32 - index * 0.03);
            dot.position.z -= index * 0.02;
            previous = dot.position.clone();
            const material = dot.material as THREE.MeshBasicMaterial;
            material.opacity = Math.max(0.08, 0.42 - index * 0.055);
          });
        }

        for (let index = ripples.length - 1; index >= 0; index -= 1) {
          const ripple = ripples[index];
          ripple.life += delta;
          const progress = ripple.life / 1.05;
          ripple.mesh.scale.setScalar(1 + progress * 3.6);
          ripple.material.opacity = Math.max(0, 0.75 * (1 - progress));
          if (progress >= 1) {
            group.remove(ripple.mesh);
            ripple.mesh.geometry.dispose();
            ripple.material.dispose();
            ripples.splice(index, 1);
          }
        }
      }

      renderer.render(scene, camera);
      frame = window.requestAnimationFrame(render);
    };

    if (reduced) {
      applyStatic();
    } else {
      render();
    }

    const resize = () => {
      const nextWidth = container.clientWidth || width;
      const nextHeight = container.clientHeight || height;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
      renderer.setSize(nextWidth, nextHeight, false);
      camera.aspect = nextWidth / nextHeight;
      baseCamera.z = fitCameraZ(camera.aspect);
      camera.position.z = baseCamera.z;
      camera.updateProjectionMatrix();
      if (reduced) applyStatic();
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      observer.disconnect();
      surface.removeEventListener('pointermove', updatePointer);
      surface.removeEventListener('pointerleave', onPointerLeave);
      surface.removeEventListener('pointerdown', onPointerDown);
      surface.style.cursor = 'default';
      ripples.forEach(({ mesh, material }) => {
        mesh.geometry.dispose();
        material.dispose();
      });
      renderer.dispose();
      scene.traverse((object) => {
        if (object instanceof THREE.Sprite) {
          object.material.map?.dispose();
          object.material.dispose();
          return;
        }
        if (
          object instanceof THREE.Mesh ||
          object instanceof THREE.Line ||
          object instanceof THREE.Points
        ) {
          object.geometry.dispose();
          const material = object.material as THREE.Material | THREE.Material[];
          if (Array.isArray(material)) material.forEach((item) => item.dispose());
          else material.dispose();
        }
      });
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
    };
  }, [dense]);

  return <div className="pipeline-scene" ref={host} aria-hidden="true" />;
}
