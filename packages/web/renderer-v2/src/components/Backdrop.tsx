import { useEffect, useRef } from 'react';
import * as THREE from 'three';

type Props = {
  dense?: boolean;
};

const STATIONS = [
  { key: 'docs', title: '招标文件', caption: 'READ', accent: '#818cf8', angle: -Math.PI * 0.72 },
  { key: 'analysis', title: 'AI 解读', caption: 'ANALYZE', accent: '#22d3ee', angle: -Math.PI * 0.24 },
  { key: 'outline', title: '标书章节', caption: 'WRITE', accent: '#c4b5fd', angle: Math.PI * 0.24 },
  { key: 'delivery', title: '交付文件', caption: 'DELIVER', accent: '#5eead4', angle: Math.PI * 0.72 },
];

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

function makeLabel(title: string, caption: string, accent: string): THREE.Sprite | null {
  const canvas = document.createElement('canvas');
  canvas.width = 384;
  canvas.height = 112;
  const context = canvas.getContext('2d');
  if (!context) return null;

  roundRect(context, 2, 2, 380, 108, 22);
  context.fillStyle = 'rgba(18, 18, 44, 0.78)';
  context.fill();
  context.strokeStyle = 'rgba(255, 255, 255, 0.18)';
  context.lineWidth = 2;
  context.stroke();

  context.fillStyle = accent;
  context.fillRect(2, 2, 6, 108);
  context.fillStyle = 'rgba(255, 255, 255, 0.92)';
  context.font = '600 30px "PingFang SC", "Microsoft YaHei", sans-serif';
  context.fillText(title, 24, 48);
  context.fillStyle = 'rgba(255, 255, 255, 0.46)';
  context.font = '18px ui-monospace, SFMono-Regular, Menlo, monospace';
  context.fillText(caption, 24, 82);

  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, opacity: 0.72 });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(2.3, 0.67, 1);
  return sprite;
}

export function Backdrop({ dense = false }: Props) {
  const host = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = host.current;
    if (!container) return;

    const compact = window.matchMedia('(max-width: 720px)').matches;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const width = container.clientWidth || window.innerWidth;
    const height = container.clientHeight || window.innerHeight;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(46, width / height, 0.1, 120);
    const baseCamera = new THREE.Vector3(0, 0.2, compact ? 21 : 17.5);
    camera.position.copy(baseCamera);
    camera.lookAt(0, 0.2, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
    renderer.setSize(width, height);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    container.appendChild(renderer.domElement);

    const group = new THREE.Group();
    const baseScale = compact ? 0.78 : 1.02;
    group.scale.setScalar(baseScale * 0.88);
    group.position.y = 0.2;
    scene.add(group);

    const colors = {
      indigo: 0x6366f1,
      violet: 0x8b5cf6,
      cyan: 0x22d3ee,
      teal: 0x0d9488,
      paper: 0xe0e7ff,
    };

    const lineMaterial = (color: number, opacity: number) =>
      new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
    const fillMaterial = (color: number, opacity: number) =>
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity, side: THREE.DoubleSide, depthWrite: false });
    const glowMaterial = (color: number, opacity: number) =>
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });

    // ── 中央“投标智能核”：文件、评分与交付围绕它运转 ──
    const core = new THREE.Mesh(
      new THREE.IcosahedronGeometry(compact ? 2.5 : 3.1, 1),
      new THREE.MeshBasicMaterial({ color: colors.violet, wireframe: true, transparent: true, opacity: 0.52 }),
    );
    group.add(core);

    const inner = new THREE.Mesh(
      new THREE.IcosahedronGeometry(compact ? 1.45 : 1.8, 1),
      new THREE.MeshBasicMaterial({ color: colors.cyan, wireframe: true, transparent: true, opacity: 0.38 }),
    );
    group.add(inner);

    const coreGlow = new THREE.Mesh(new THREE.SphereGeometry(compact ? 2.7 : 3.3, 24, 24), glowMaterial(colors.violet, 0.045));
    group.add(coreGlow);

    const rings: THREE.Mesh[] = [];
    const ringRadii = compact ? [4.8, 6.0] : [5.8, 7.2, 8.8];
    ringRadii.forEach((radius, index) => {
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(radius, 0.028, 8, 180),
        lineMaterial([colors.indigo, colors.cyan, colors.violet][index], 0.32),
      );
      ring.rotation.x = Math.PI / 2.25 + index * 0.32;
      ring.rotation.y = index * 0.3;
      rings.push(ring);
      group.add(ring);
    });

    const scoreRing = new THREE.Mesh(
      new THREE.TorusGeometry(compact ? 4.2 : 5.1, 0.018, 8, 140, Math.PI * 1.25),
      glowMaterial(colors.cyan, 0.45),
    );
    scoreRing.rotation.set(Math.PI / 2.1, 0.2, 0.4);
    group.add(scoreRing);

    const tickGroup = new THREE.Group();
    const tickCount = compact ? 16 : 28;
    const tickGeometry = new THREE.PlaneGeometry(0.035, 0.24);
    for (let index = 0; index < tickCount; index += 1) {
      const angle = (index / tickCount) * Math.PI * 2;
      const tick = new THREE.Mesh(tickGeometry, fillMaterial(colors.cyan, index % 4 === 0 ? 0.5 : 0.2));
      const radius = compact ? 4.25 : 5.15;
      tick.position.set(Math.cos(angle) * radius, Math.sin(angle) * radius, 0);
      tick.rotation.z = angle;
      tickGroup.add(tick);
    }
    tickGroup.rotation.x = Math.PI / 2.1;
    tickGroup.rotation.z = 0.4;
    group.add(tickGroup);

    // ── 四个业务站点：沿着椭圆轨道环绕 ──
    const orbit = new THREE.Group();
    group.add(orbit);
    const stations: Record<string, THREE.Group> = {};
    const stationList: THREE.Group[] = [];
    const labels: THREE.Sprite[] = [];

    const registerStation = (key: string, station: THREE.Group, angle: number) => {
      station.userData.station = key;
      station.userData.hoverScale = 1;
      station.userData.pulse = 0;
      const radiusX = compact ? 5.6 : 7.4;
      const radiusZ = compact ? 3.6 : 4.8;
      station.position.set(Math.cos(angle) * radiusX, Math.sin(angle) * 1.35 + 0.25, Math.sin(angle) * radiusZ);
      const halo = new THREE.Mesh(new THREE.TorusGeometry(compact ? 0.9 : 1.2, 0.012, 8, 64), glowMaterial(colors.cyan, 0));
      halo.position.z = -0.08;
      station.add(halo);
      station.userData.halo = halo;
      stations[key] = station;
      stationList.push(station);
      orbit.add(station);

      const spec = STATIONS.find((item) => item.key === key);
      if (!compact && spec) {
        const sprite = makeLabel(spec.title, spec.caption, spec.accent);
        if (sprite) {
          sprite.position.set(0, 1.55, 0);
          station.add(sprite);
          labels.push(sprite);
        }
      }
    };

    const docs = new THREE.Group();
    const pageGeo = new THREE.PlaneGeometry(1.55, 2.0);
    const pageEdges = new THREE.EdgesGeometry(pageGeo);
    for (let index = 0; index < 3; index += 1) {
      const page = new THREE.Mesh(pageGeo, fillMaterial(colors.paper, index === 2 ? 0.42 : 0.26));
      page.position.set(index * 0.14, index * 0.1, -index * 0.04);
      page.rotation.z = index * 0.035;
      docs.add(page);
      const edges = new THREE.LineSegments(pageEdges, lineMaterial(colors.indigo, 0.52));
      edges.position.copy(page.position);
      edges.rotation.copy(page.rotation);
      docs.add(edges);
    }
    for (let line = 0; line < 4; line += 1) {
      const rule = new THREE.Mesh(new THREE.PlaneGeometry(line === 0 ? 0.7 : 1.0, 0.025), fillMaterial(colors.indigo, 0.24));
      rule.position.set(0.08, 0.62 - line * 0.32, 0.12);
      docs.add(rule);
    }
    registerStation('docs', docs, STATIONS[0].angle);

    const analysis = new THREE.Group();
    const analysisRing = new THREE.Mesh(new THREE.TorusGeometry(0.86, 0.022, 8, 80), lineMaterial(colors.cyan, 0.6));
    analysis.add(analysisRing);
    const analysisCore = new THREE.Mesh(new THREE.IcosahedronGeometry(0.38, 0), lineMaterial(colors.violet, 0.55));
    analysis.add(analysisCore);
    for (let index = 0; index < 6; index += 1) {
      const angle = (index / 6) * Math.PI * 2;
      const node = new THREE.Mesh(
        new THREE.SphereGeometry(0.055, 8, 8),
        fillMaterial(index % 2 === 0 ? colors.cyan : colors.indigo, 0.8),
      );
      node.position.set(Math.cos(angle) * 0.86, Math.sin(angle) * 0.86, 0);
      analysis.add(node);
    }
    const bars: THREE.Mesh[] = [];
    for (let index = 0; index < 4; index += 1) {
      const height = 0.26 + index * 0.12;
      const bar = new THREE.Mesh(new THREE.BoxGeometry(0.055, height, 0.03), fillMaterial(colors.indigo, 0.5));
      bar.position.set(1.22, -0.5 + height / 2, 0);
      bars.push(bar);
    }
    bars.forEach((bar) => analysis.add(bar));
    registerStation('analysis', analysis, STATIONS[1].angle);

    const outline = new THREE.Group();
    const cells: THREE.Mesh[] = [];
    for (let row = 0; row < 2; row += 1) {
      for (let column = 0; column < 3; column += 1) {
        const geometry = new THREE.PlaneGeometry(0.42, 0.28);
        const cell = new THREE.Mesh(geometry, fillMaterial(colors.paper, 0.28));
        cell.position.set((column - 1) * 0.5, (row - 0.5) * 0.36, 0);
        outline.add(cell);
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), lineMaterial(colors.violet, 0.48));
        edges.position.copy(cell.position);
        outline.add(edges);
        const glow = new THREE.Mesh(new THREE.PlaneGeometry(0.32, 0.2), glowMaterial(colors.violet, 0.1));
        glow.position.set(cell.position.x, cell.position.y, 0.01);
        outline.add(glow);
        cells.push(glow);
      }
    }
    registerStation('outline', outline, STATIONS[2].angle);

    const delivery = new THREE.Group();
    const finalGeo = new THREE.PlaneGeometry(1.35, 1.75);
    const finalPage = new THREE.Mesh(finalGeo, fillMaterial(colors.paper, 0.32));
    delivery.add(finalPage);
    delivery.add(new THREE.LineSegments(new THREE.EdgesGeometry(finalGeo), lineMaterial(colors.teal, 0.55)));
    for (let line = 0; line < 4; line += 1) {
      const rule = new THREE.Mesh(new THREE.PlaneGeometry(line === 0 ? 0.55 : 0.95, 0.022), fillMaterial(colors.indigo, 0.22));
      rule.position.set(0.08, 0.58 - line * 0.3, 0.01);
      delivery.add(rule);
    }
    const seal = new THREE.Mesh(new THREE.TorusGeometry(0.3, 0.02, 8, 48), lineMaterial(colors.teal, 0.7));
    seal.position.set(0.4, -0.48, 0.02);
    delivery.add(seal);
    const sealGlow = new THREE.Mesh(new THREE.TorusGeometry(0.32, 0.014, 8, 56), glowMaterial(colors.teal, 0.16));
    sealGlow.position.copy(seal.position);
    delivery.add(sealGlow);
    const check = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0.28, -0.45, 0.03),
      new THREE.Vector3(0.4, -0.58, 0.03),
      new THREE.Vector3(0.6, -0.3, 0.03),
    ]);
    delivery.add(new THREE.Line(check, lineMaterial(colors.teal, 0.85)));
    registerStation('delivery', delivery, STATIONS[3].angle);

    // ── 卫星与粒子 ──
    const satellites: THREE.Mesh[] = [];
    for (let index = 0; index < (compact ? 3 : 6); index += 1) {
      const satellite = new THREE.Mesh(
        new THREE.SphereGeometry(0.055, 10, 10),
        glowMaterial(index % 2 === 0 ? colors.cyan : colors.violet, 0.62),
      );
      group.add(satellite);
      satellites.push(satellite);
    }

    const particleCount = compact ? 260 : dense ? 760 : 480;
    const particlePositions = new Float32Array(particleCount * 3);
    for (let index = 0; index < particleCount; index += 1) {
      particlePositions[index * 3] = (Math.random() - 0.5) * 34;
      particlePositions[index * 3 + 1] = (Math.random() - 0.5) * 24;
      particlePositions[index * 3 + 2] = (Math.random() - 0.5) * 20;
    }
    const particleGeo = new THREE.BufferGeometry();
    particleGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));
    const particles = new THREE.Points(
      particleGeo,
      new THREE.PointsMaterial({ color: 0xc7d2fe, size: 0.05, transparent: true, opacity: 0.55, depthWrite: false }),
    );
    scene.add(particles);

    // ── 鼠标 / 触屏交互 ──
    const cursorOrb = new THREE.Mesh(new THREE.SphereGeometry(0.1, 14, 14), glowMaterial(colors.cyan, 0.75));
    cursorOrb.visible = false;
    if (!compact) scene.add(cursorOrb);

    const trail: THREE.Mesh[] = [];
    if (!compact) {
      for (let index = 0; index < 6; index += 1) {
        const dot = new THREE.Mesh(
          new THREE.SphereGeometry(0.032 - index * 0.003, 8, 8),
          glowMaterial(index % 2 === 0 ? colors.cyan : colors.violet, 0.45 - index * 0.05),
        );
        dot.visible = false;
        scene.add(dot);
        trail.push(dot);
      }
    }

    const pointer = { x: 0, y: 0, active: false };
    const cursorTarget = new THREE.Vector3(0, 0, 3);
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    const hitPoint = new THREE.Vector3();
    let hovered = '';
    let autoSpin = 0;

    const updatePointer = (event: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      pointer.x = (px - 0.5) * 2;
      pointer.y = (py - 0.5) * 2;
      pointer.active = true;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      cursorTarget.set(pointer.x * 13, -pointer.y * 7, 2.6);

      raycaster.setFromCamera(ndc, camera);
      const hits = raycaster.intersectObjects(stationList, true);
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
      container.style.cursor = hovered ? 'pointer' : 'default';
    };

    type Ripple = { mesh: THREE.Mesh; material: THREE.MeshBasicMaterial; life: number };
    const ripples: Ripple[] = [];

    const onPointerDown = (event: PointerEvent) => {
      if (reduced) return;
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = (event.clientX - rect.left) / rect.width;
      const py = (event.clientY - rect.top) / rect.height;
      ndc.set(px * 2 - 1, -(py * 2 - 1));
      raycaster.setFromCamera(ndc, camera);
      if (!raycaster.ray.intersectPlane(plane, hitPoint)) return;
      if (hovered && stations[hovered]) stations[hovered].userData.pulse = 1;

      if (ripples.length >= 4) return;
      const material = glowMaterial(colors.cyan, 0.72);
      const mesh = new THREE.Mesh(new THREE.TorusGeometry(0.2, 0.012, 8, 64), material);
      const local = group.worldToLocal(hitPoint.clone());
      mesh.position.set(local.x, local.y, local.z + 0.2);
      group.add(mesh);
      ripples.push({ mesh, material, life: 0 });
    };

    const onPointerLeave = () => {
      pointer.active = false;
      hovered = '';
      cursorOrb.visible = false;
      trail.forEach((dot) => {
        dot.visible = false;
      });
      container.style.cursor = 'default';
    };

    container.addEventListener('pointermove', updatePointer);
    container.addEventListener('pointerdown', onPointerDown);
    container.addEventListener('pointerleave', onPointerLeave);

    const clock = new THREE.Clock();
    let frame = 0;
    let running = true;

    const renderStatic = () => {
      group.scale.setScalar(baseScale);
      renderer.render(scene, camera);
    };

    const render = () => {
      if (!running) return;
      const delta = Math.min(0.06, clock.getDelta());
      const time = clock.elapsedTime;
      const intro = Math.min(1, time / 1.6);
      const eased = 1 - Math.pow(1 - intro, 3);
      group.scale.setScalar(baseScale * (0.88 + 0.12 * eased));

      if (!reduced) {
        autoSpin += delta * 0.045;
        group.rotation.y += (autoSpin + pointer.x * 0.16 - group.rotation.y) * 0.08;
        group.rotation.x += (-pointer.y * 0.1 - group.rotation.x) * 0.06;
        group.position.y = 0.2 + Math.sin(time * 0.55) * 0.08;

        core.rotation.y = time * 0.32;
        core.rotation.x = time * 0.16;
        inner.rotation.y = -time * 0.42;
        inner.rotation.z = time * 0.2;
        coreGlow.scale.setScalar(1 + Math.sin(time * 1.1) * 0.08);
        rings.forEach((ring, index) => {
          ring.rotation.z += delta * (0.03 + index * 0.012);
        });
        scoreRing.rotation.z = time * 0.12;
        tickGroup.rotation.z = 0.4 + time * 0.06;
        satellites.forEach((satellite, index) => {
          const angle = time * 0.38 + (index / satellites.length) * Math.PI * 2;
          satellite.position.set(Math.cos(angle) * 6.4, Math.sin(angle * 1.3) * 2.2, Math.sin(angle) * 4.4);
        });
        particles.rotation.y += delta * 0.025;
        particles.rotation.x = Math.sin(time * 0.1) * 0.03;

        stationList.forEach((station) => {
          station.lookAt(camera.position);
          const key = String(station.userData.station || '');
          const target = key === hovered ? 1.16 : 1;
          const current = Number(station.userData.hoverScale || 1);
          station.userData.hoverScale = current + (target - current) * 0.1;
          const pulse = Number(station.userData.pulse || 0);
          station.scale.setScalar(station.userData.hoverScale + pulse * 0.08);
          station.userData.pulse = Math.max(0, pulse - delta * 2.2);
          const halo = station.userData.halo as THREE.Mesh | undefined;
          if (halo) {
            const material = halo.material as THREE.MeshBasicMaterial;
            const targetOpacity = key === hovered ? 0.55 : 0;
            material.opacity += (targetOpacity - material.opacity) * 0.12;
          }
        });

        cells.forEach((cell, index) => {
          const material = cell.material as THREE.MeshBasicMaterial;
          material.opacity = 0.08 + ((Math.sin(time * 1.1 + index * 0.4) + 1) / 2) * 0.26;
        });
        bars.forEach((bar, index) => {
          bar.scale.y = 1 + Math.sin(time * 1.3 + index * 0.5) * 0.2;
        });
        sealGlow.scale.setScalar(1 + Math.sin(time * 1.5) * 0.1);
        const sealMaterial = sealGlow.material as THREE.MeshBasicMaterial;
        sealMaterial.opacity = 0.1 + (Math.sin(time * 1.5) + 1) * 0.1;

        camera.position.x += (baseCamera.x + pointer.x * (compact ? 0.3 : 0.9) - camera.position.x) * 0.06;
        camera.position.y += (baseCamera.y - pointer.y * (compact ? 0.2 : 0.55) - camera.position.y) * 0.06;
        camera.lookAt(0, 0.2, 0);

        if (!compact && (cursorOrb.visible || pointer.active)) {
          cursorOrb.visible = true;
          cursorOrb.position.lerp(cursorTarget, 0.16);
          cursorOrb.position.z = 2.6 + Math.sin(time * 2.2) * 0.1;
        }
        if (!compact) {
          let previous = cursorOrb.position.clone();
          trail.forEach((dot, index) => {
            if (!pointer.active) return;
            dot.visible = true;
            dot.position.lerp(previous, 0.3 - index * 0.03);
            dot.position.z -= index * 0.03;
            previous = dot.position.clone();
            const material = dot.material as THREE.MeshBasicMaterial;
            material.opacity = Math.max(0.08, 0.38 - index * 0.05);
          });
        }
      }

      for (let index = ripples.length - 1; index >= 0; index -= 1) {
        const ripple = ripples[index];
        ripple.life += delta;
        const progress = ripple.life / 1.1;
        ripple.mesh.scale.setScalar(1 + progress * 4.2);
        ripple.material.opacity = Math.max(0, 0.72 * (1 - progress));
        if (progress >= 1) {
          group.remove(ripple.mesh);
          ripple.mesh.geometry.dispose();
          ripple.material.dispose();
          ripples.splice(index, 1);
        }
      }

      renderer.render(scene, camera);
      frame = window.requestAnimationFrame(render);
    };

    if (reduced) {
      renderStatic();
    } else {
      render();
    }

    const resize = () => {
      const nextWidth = container.clientWidth || width;
      const nextHeight = container.clientHeight || height;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compact ? 1.5 : 2));
      renderer.setSize(nextWidth, nextHeight, false);
      camera.aspect = nextWidth / nextHeight;
      camera.updateProjectionMatrix();
      if (reduced) renderStatic();
    };
    window.addEventListener('resize', resize);
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      window.removeEventListener('resize', resize);
      container.removeEventListener('pointermove', updatePointer);
      container.removeEventListener('pointerdown', onPointerDown);
      container.removeEventListener('pointerleave', onPointerLeave);
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
        if (object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.Points) {
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

  return <div ref={host} className="backdrop" aria-hidden="true" />;
}
