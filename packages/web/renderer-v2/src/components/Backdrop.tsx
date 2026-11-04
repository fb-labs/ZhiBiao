import { useEffect, useRef } from 'react';
import * as THREE from 'three';

type Props = {
  dense?: boolean;
};

export function Backdrop({ dense = false }: Props) {
  const host = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = host.current;
    if (!container) return;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 100);
    camera.position.set(0, 0, 16);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(container.clientWidth, container.clientHeight);
    container.appendChild(renderer.domElement);

    const group = new THREE.Group();
    scene.add(group);

    const coreGeo = new THREE.IcosahedronGeometry(4.4, 1);
    const core = new THREE.Mesh(
      coreGeo,
      new THREE.MeshBasicMaterial({ color: 0x8b5cf6, wireframe: true, transparent: true, opacity: 0.55 }),
    );
    group.add(core);

    const inner = new THREE.Mesh(
      new THREE.IcosahedronGeometry(2.6, 1),
      new THREE.MeshBasicMaterial({ color: 0x22d3ee, wireframe: true, transparent: true, opacity: 0.35 }),
    );
    group.add(inner);

    const rings: THREE.Mesh[] = [];
    [6.2, 7.6, 9.2].forEach((radius, index) => {
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(radius, 0.035, 8, 160),
        new THREE.MeshBasicMaterial({
          color: [0x818cf8, 0x38bdf8, 0x22d3ee][index],
          transparent: true,
          opacity: 0.35,
        }),
      );
      ring.rotation.x = Math.PI / 2.2 + index * 0.35;
      ring.rotation.y = index * 0.25;
      rings.push(ring);
      group.add(ring);
    });

    const pages: THREE.Mesh[] = [];
    for (let i = 0; i < 7; i += 1) {
      const page = new THREE.Mesh(
        new THREE.PlaneGeometry(1.8, 2.5),
        new THREE.MeshBasicMaterial({
          color: 0xe0e7ff,
          wireframe: true,
          transparent: true,
          opacity: 0.22,
          side: THREE.DoubleSide,
        }),
      );
      const angle = (i / 7) * Math.PI * 2;
      const radius = 5.4 + (i % 3) * 0.9;
      page.position.set(Math.cos(angle) * radius, Math.sin(angle * 1.4) * 1.8, Math.sin(angle) * radius);
      page.rotation.set(angle * 0.6, angle, 0.2);
      pages.push(page);
      group.add(page);
    }

    const count = dense ? 900 : 520;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      positions[i * 3] = (Math.random() - 0.5) * 34;
      positions[i * 3 + 1] = (Math.random() - 0.5) * 24;
      positions[i * 3 + 2] = (Math.random() - 0.5) * 20;
    }
    const particleGeo = new THREE.BufferGeometry();
    particleGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const particles = new THREE.Points(
      particleGeo,
      new THREE.PointsMaterial({ color: 0xc7d2fe, size: 0.05, transparent: true, opacity: 0.6 }),
    );
    scene.add(particles);

    const pointer = { x: 0, y: 0 };
    const onMove = (event: PointerEvent) => {
      pointer.x = (event.clientX / window.innerWidth - 0.5) * 0.6;
      pointer.y = (event.clientY / window.innerHeight - 0.5) * 0.4;
    };
    window.addEventListener('pointermove', onMove);

    let frame = 0;
    let raf = 0;
    const animate = () => {
      frame += 1;
      group.rotation.y += 0.0016;
      group.rotation.x = Math.sin(frame * 0.0015) * 0.08;
      rings.forEach((ring, index) => {
        ring.rotation.z += 0.002 + index * 0.0012;
      });
      pages.forEach((page, index) => {
        page.position.y = Math.sin(frame * 0.002 + index) * 1.9;
        page.rotation.y += 0.003;
      });
      particles.rotation.y += 0.0006;
      camera.position.x += (pointer.x * 4 - camera.position.x) * 0.02;
      camera.position.y += (-pointer.y * 3 - camera.position.y) * 0.02;
      camera.lookAt(0, 0, 0);
      renderer.render(scene, camera);
      raf = requestAnimationFrame(animate);
    };
    animate();

    const onResize = () => {
      if (!container) return;
      camera.aspect = container.clientWidth / container.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(container.clientWidth, container.clientHeight);
    };
    window.addEventListener('resize', onResize);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('resize', onResize);
      renderer.dispose();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.Points) {
          object.geometry.dispose();
          const material = object.material as THREE.Material | THREE.Material[];
          if (Array.isArray(material)) material.forEach((item) => item.dispose());
          else material.dispose();
        }
      });
      container.innerHTML = '';
    };
  }, [dense]);

  return <div ref={host} className="backdrop" aria-hidden="true" />;
}
